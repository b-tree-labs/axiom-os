import forms from '@tailwindcss/forms';

/** @type {import('tailwindcss').Config} */
export default {
  content: ['./src/**/*.{js,jsx,ts,tsx}', './index.html'],
  theme: {
    extend: {
      colors: {
        // Theme-aware colors — values come from CSS variables in index.css.
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
  plugins: [forms],
};
