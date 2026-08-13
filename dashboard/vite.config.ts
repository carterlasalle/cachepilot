import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The dashboard frontend talks to the read-only backend
// (dashboard/backend/server.py) through the /api proxy. Defaults match the
// runbook in docs/dashboard.md: backend on 127.0.0.1:8788, dev server on
// 127.0.0.1:5173. The backend also serves this app's production build
// (dist/) when running `yarn build` first.
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8788',
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
});
