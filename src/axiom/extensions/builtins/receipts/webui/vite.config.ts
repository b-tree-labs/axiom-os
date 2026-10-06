// Copyright (c) 2026 The University of Texas at Austin
// Copyright (c) 2026 B-Tree Labs
// SPDX-License-Identifier: Apache-2.0
//
// Served from the composed node under /receipts/ (spec-receipts-surface §3):
// base makes every built asset URL live under the mount. The dev server
// proxies /api and /_appkit to a running node so `npm run dev` exercises the
// same routes the deployed page uses.
//
// APPKIT IS NOT BUNDLED. The node already serves it, with its React, at the
// URL below — that is what appkit's runtime build exists for.
//
// This console used to bundle it, and the cost was not theoretical. An appkit
// change reached this page only through a rebuild and a merge here, so the
// committed bundle sat four days behind a surface that was already written,
// and nothing on the page could say it was behind. A sibling consumer had
// already moved to this arrangement and was tracking appkit live; upgrading
// the node upgraded it, and this one stayed frozen at whatever was last built.
//
// Bundling also put a SECOND REACT on the page, which fails at the first hook
// with an error naming neither cause, and a second copy of the design system
// in the tree — the thing that produces two appkit versions in one page and
// reads as "no styling".
//
// React maps to the same URL so there is exactly one of it. Types still come
// from the `file:` dependency, which is the same wheel the node serves.
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const APPKIT_URL = '/_appkit/appkit.js';

export default defineConfig({
  base: '/receipts/',
  plugins: [react()],
  build: {
    outDir: 'dist',
    rollupOptions: {
      external: ['@axiom/appkit', 'react', 'react-dom', 'react-dom/client'],
      output: {
        // The bare specifier the source imports becomes the URL the browser
        // fetches. All four resolve to one module, so one React.
        paths: {
          '@axiom/appkit': APPKIT_URL,
          react: APPKIT_URL,
          'react-dom': APPKIT_URL,
          'react-dom/client': APPKIT_URL,
        },
      },
    },
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8768',
      '/_appkit': 'http://127.0.0.1:8768',
    },
  },
});
