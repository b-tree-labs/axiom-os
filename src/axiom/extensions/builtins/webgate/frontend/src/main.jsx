import React from 'react';
import ReactDOM from 'react-dom/client';
import './index.css';
import App from './App';
import { getBrand, applyBrandAccent } from './lib/brand';

// Optional per-consumer accent override from the server-injected brand. Absent →
// the CSS burnt-orange default stands. Runs before render so there is no flash.
applyBrandAccent(getBrand().accent);

// Input modality: reflect on <html data-input-mode> whether the user is driving
// with the keyboard or a pointer, so keyboard users get a clear focus ring (see
// index.css) and pointer users get a clean, ring-free UI. Plain DOM listeners so
// there is nothing to mis-order.
(() => {
  const el = document.documentElement;
  const set = (mode) => {
    if (el.dataset.inputMode !== mode) el.dataset.inputMode = mode;
  };
  set('pointer');
  const NAV_KEYS = new Set([
    'Tab',
    'ArrowUp',
    'ArrowDown',
    'ArrowLeft',
    'ArrowRight',
    'Home',
    'End',
    'Escape',
  ]);
  window.addEventListener('keydown', (e) => NAV_KEYS.has(e.key) && set('keyboard'), true);
  ['pointerdown', 'mousedown', 'touchstart'].forEach((evt) =>
    window.addEventListener(evt, () => set('pointer'), true),
  );
})();

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
