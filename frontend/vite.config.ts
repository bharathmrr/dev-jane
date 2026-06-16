import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Served by FastAPI at /app/ (the existing vanilla-JS dashboard stays at
// /api/v1/dashboard/ until this reaches parity — see Stage 1.10 cutover).
export default defineConfig({
  base: '/app/',
  plugins: [react(), tailwindcss()],
  build: { outDir: 'dist', emptyOutDir: true },
})
