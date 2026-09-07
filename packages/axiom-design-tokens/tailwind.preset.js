// Copyright (c) 2026 The University of Texas at Austin
// Copyright (c) 2026 B-Tree Labs
// SPDX-License-Identifier: Apache-2.0

/**
 * @axiom/design-tokens — Tailwind preset.
 *
 * Maps Tailwind's theme keys onto the CSS custom properties declared in
 * tokens.css, so `bg-theme-panel`, `text-theme-muted`, `border-theme`, ...
 * follow the active [data-theme] at runtime with no rebuild. Every colour
 * below resolves through `var(--theme-...)`; the VALUES live in tokens.css.
 *
 * Consume from a product's tailwind.config.js:
 *
 *   import tokens from '<relative path>/packages/axiom-design-tokens/tailwind.preset.js';
 *   export default { presets: [tokens], content: [...], plugins: [...] };
 *
 * CommonJS on purpose: Tailwind 3 loads configs through jiti, so both
 * `require()` and `import` of this file work whatever the consumer's module
 * type. No `content`, no `plugins`, no dependencies: those stay with the
 * product (webgate keeps @tailwindcss/forms).
 */

/** @type {import('tailwindcss').Config} */
module.exports = {
  theme: {
    extend: {
      colors: {
        // Theme-aware colors — values come from CSS variables in tokens.css.
        // Edit the tokens there to reskin light/dark; the class names here are
        // kept identical to the portable component set so markup lifts unchanged.
        theme: {
          page: 'var(--theme-page-bg)',
          sidebar: 'var(--theme-sidebar-bg)',
          panel: 'var(--theme-panel-bg)',
          surface: 'var(--theme-surface)',
          'surface-elevated': 'var(--theme-surface-elevated)',
          'surface-subtle': 'var(--theme-surface-subtle)',
          'surface-hover': 'var(--theme-nav-hover-solid)',
          'input-bg': 'var(--theme-input-bg)',
          'input-border': 'var(--theme-input-border)',
          'input-placeholder': 'var(--theme-input-placeholder)',
          primary: 'var(--theme-text-primary)',
          secondary: 'var(--theme-text-secondary)',
          muted: 'var(--theme-text-muted)',
          border: 'var(--theme-border)',
          'border-subtle': 'var(--theme-border-subtle)',
          accent: 'var(--theme-accent)',
          'accent-hover': 'var(--theme-accent-hover)',
          'accent-strong': 'var(--theme-accent-strong)',
          'on-accent': 'var(--theme-on-accent)',
          'accent-text': 'var(--theme-accent-text)',
          'nav-hover': 'var(--theme-nav-hover)',
          'nav-active': 'var(--theme-nav-active)',
        },
        // Status colors — brand-neutral standards, referenced by the alert markup.
        'status-success': '#16a34a',
        'status-warning': '#d97706',
        'status-error': '#dc2626',
        'status-info': '#2563eb',
      },
      // Mirrors --app-font-sans in tokens.css: system UI fonts, nothing fetched.
      fontFamily: {
        sans: [
          '-apple-system',
          'BlinkMacSystemFont',
          'Segoe UI',
          'Roboto',
          'Helvetica',
          'Arial',
          'sans-serif',
        ],
      },
      borderRadius: {
        sm: '4px',
        DEFAULT: '6px',
        md: '8px',
        lg: '10px',
        xl: '14px',
      },
    },
  },
};
