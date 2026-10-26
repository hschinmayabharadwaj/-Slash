import { fileURLToPath } from 'node:url';
import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// In development, run the FastAPI server on :8765 and open the Vite dev server on
// :5173. Set web.baseUrl to http://localhost:5173 (and the GitHub App callback
// to http://localhost:5173/auth/callback) so cookies and Host checks line up.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8765', changeOrigin: false },
      '/auth': { target: 'http://127.0.0.1:8765', changeOrigin: false },
    },
  },
  build: { outDir: 'dist', sourcemap: false },
});
