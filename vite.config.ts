import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { VitePWA } from "vite-plugin-pwa";

// base must match the GitHub Pages repo path so asset URLs resolve on Pages.
const base = process.env.GITHUB_PAGES ? "/ticket-desk/" : "/";

// Stamped into the bundle so the UI can show which build is running. ops#18 asks for
// this regardless of the update strategy: it makes staleness visible rather than
// invisible, which is the actual failure mode.
const buildTime = new Date().toISOString();

export default defineConfig({
  base,
  define: {
    __BUILD_TIME__: JSON.stringify(buildTime),
  },
  plugins: [
    react(),
    tailwindcss(),
    VitePWA({
      // "prompt", not "autoUpdate". With autoUpdate a new worker installs in the
      // background and activates on the NEXT visit, so the first load after any deploy
      // silently shows the previous version. That was tolerable while the app only did
      // arithmetic on values the user typed in; ops#18 said it becomes important the
      // moment collected market data drives the display, and as of the TickPick
      // collector it does. A dashboard quietly out of date is worse than one obviously
      // broken - especially at a T-48h deadline, where the decision is irreversible.
      //
      // skipWaiting was the other candidate and is rejected: it can swap assets
      // mid-session. An explicit prompt lets the user see that something changed.
      registerType: "prompt",
      injectRegister: "auto",
      includeAssets: ["icon-192.png", "icon-512.png"],
      manifest: {
        name: "Ticket Desk",
        short_name: "Ticket Desk",
        description: "Season ticket resale vs. exchange decisions",
        // Relative so the installed app resolves correctly under the Pages subpath.
        start_url: ".",
        scope: ".",
        display: "standalone",
        orientation: "portrait",
        theme_color: "#006d75",
        background_color: "#020617",
        icons: [
          { src: "icon-192.png", sizes: "192x192", type: "image/png", purpose: "any" },
          { src: "icon-512.png", sizes: "512x512", type: "image/png", purpose: "any" },
          { src: "icon-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
        ],
      },
      workbox: {
        // The app is fully static and its data is a committed JSON file, so precaching
        // everything makes it work offline - which matters at the arena.
        globPatterns: ["**/*.{js,css,html,png,json,webmanifest}"],
        cleanupOutdatedCaches: true,
      },
    }),
  ],
});
