import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { VitePWA } from "vite-plugin-pwa";

// base must match the GitHub Pages repo path so asset URLs resolve on Pages.
const base = process.env.GITHUB_PAGES ? "/ticket-desk/" : "/";

export default defineConfig({
  base,
  plugins: [
    react(),
    tailwindcss(),
    VitePWA({
      registerType: "autoUpdate",
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
