// Copyright (c) 2026 The University of Texas at Austin
// Copyright (c) 2026 B-Tree Labs
// SPDX-License-Identifier: Apache-2.0
//
// Served from the composed node under /receipts/ (spec-receipts-surface §3):
// base makes every built asset URL live under the mount. The dev server
// proxies /api and /_appkit to a running node so `npm run dev` exercises the
// same routes the deployed page uses.
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  base: '/receipts/',
  plugins: [react()],
  build: { outDir: 'dist' },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8768',
      '/_appkit': 'http://127.0.0.1:8768',
    },
  },
});
