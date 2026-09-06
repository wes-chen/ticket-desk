/**
 * JPEG -> PNG at native pixel size, so extract_price_bands.py can read a chart that
 * arrives as a JPEG. ops#19, ops#31.
 *
 * WHY CHROMIUM FOR SOMETHING THIS TRIVIAL. It is the only image decoder on this
 * machine. There is no PIL, no numpy, no ImageMagick and no pdftoppm, and
 * scripts/lib/minipng.py deliberately reads PNG only - a pure-Python JPEG decoder
 * would be hundreds of lines of DCT for a one-off conversion. Playwright is already a
 * dependency for the source probes, so this borrows a decoder rather than adding one.
 *
 * WHY THE EXACT-SIZE VIEWPORT AND deviceScaleFactor 1. The extractor resolves bands
 * that are a handful of pixels wide - ops#31 traced the 1020x1320 chart's failures to
 * single-row bands being ~4px. Any resampling here would blur precisely those edges
 * and the extraction would then be measuring Chromium's interpolation. `image-rendering:
 * pixelated` and a clip at natural size keep it pixel-for-pixel; a mismatch between the
 * stated dimensions and the file's own is therefore an error, not something to scale away.
 */
import { chromium } from "playwright";
import { readFileSync } from "node:fs";

const [src, out, w, h] = process.argv.slice(2);
if (!src || !out || !w || !h) {
  console.error("usage: node scripts/jpeg_to_png.mjs IN.jpg OUT.png WIDTH HEIGHT");
  process.exit(2);
}
const W = Number(w), H = Number(h);

const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: W, height: H }, deviceScaleFactor: 1 });

  // Inlined as a data URI rather than referenced as file://. A page built with
  // setContent has an about:blank origin, and Chromium refuses file:// subresources
  // from it - the img stays 0x0 and the screenshot would have been a blank white page
  // that looks like a successful conversion.
  const uri = `data:image/jpeg;base64,${readFileSync(src).toString("base64")}`;
  await page.setContent(
    `<style>html,body{margin:0;padding:0;background:#fff}` +
    `img{display:block;width:${W}px;height:${H}px;image-rendering:pixelated}</style>` +
    `<img id="c">`);
  const nat = await page.evaluate(async (u) => {
    const img = document.getElementById("c");
    img.src = u;
    await img.decode();          // resolves only once the pixels really exist
    return [img.naturalWidth, img.naturalHeight];
  }, uri);

  // Verify the decode matched the dimensions we were told, rather than assuming it.
  // A silent mismatch would mean the PNG is a scaled copy and every band edge in it
  // is interpolated - the failure this file exists to avoid.
  if (nat[0] !== W || nat[1] !== H) {
    throw new Error(`image is ${nat[0]}x${nat[1]} but ${W}x${H} was requested - refusing to resample`);
  }

  await page.screenshot({ path: out, clip: { x: 0, y: 0, width: W, height: H } });
  console.log(`wrote ${out} at ${W}x${H}`);
} finally {
  await browser.close();
}
