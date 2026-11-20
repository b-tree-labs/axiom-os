import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The gate (webgate router) serves the JSON auth seam. In `npm run dev` we proxy
// the gate endpoints to a locally running backend so the login flow works against
// real sessions without CORS. Point these at wherever `axi serve` binds the gate.
const GATE_TARGET = process.env.AXIOM_GATE_TARGET || 'http://127.0.0.1:8799';

// https://vitejs.dev/config/
export default defineConfig({
  // The gate serves this SPA under `/gate/` (react-router basename="/gate"), so
  // build asset URLs and the dev server both live under that base. In `npm run
  // dev` the app is at http://localhost:5273/gate/login.
  base: '/gate/',
  plugins: [react()],
  server: {
    port: 5273,
    proxy: {
      '/gate/session': GATE_TARGET,
      '/gate/me': GATE_TARGET,
      '/gate/logout': GATE_TARGET,
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
});
