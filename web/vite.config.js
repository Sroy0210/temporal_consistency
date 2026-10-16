import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development, the page runs on Vite (port 5173) and API calls go to FastAPI (port 8000).
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://localhost:8000" } },
});
