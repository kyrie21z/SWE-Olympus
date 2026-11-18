import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
const backendUrl = process.env.OLYMPUS_BACKEND_URL || "http://127.0.0.1:8000";
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5175,
    strictPort: true,
    proxy: {
      "/health": backendUrl,
      "/api": backendUrl,
    },
  },
});
