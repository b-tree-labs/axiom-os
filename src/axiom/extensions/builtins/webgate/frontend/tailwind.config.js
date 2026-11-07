import forms from '@tailwindcss/forms';
// Colours (var(--theme-*)), status colours, the system font stack and the radius
// scale come from the shared design-tokens preset; the token VALUES live in
// packages/axiom-design-tokens/tokens.css (imported by src/index.css).
import tokens from '../../../../../../packages/axiom-design-tokens/tailwind.preset.js';

/** @type {import('tailwindcss').Config} */
export default {
  presets: [tokens],
  content: ['./src/**/*.{js,jsx,ts,tsx}', './index.html'],
  plugins: [forms],
};
