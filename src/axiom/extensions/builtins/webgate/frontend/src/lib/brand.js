/**
 * Runtime brand.
 *
 * The built bundle is brand-neutral: it ships ONE `dist/` and reads its identity
 * at runtime from a global the webgate backend injects into <head> before the
 * module script runs:
 *
 *   window.__AXIOM_GATE_BRAND__ = { productName, tagline, logoSvg, accent, footer }
 *
 * (built from the server's `LoginBrand`). When the global is absent — dev server,
 * or a deploy that didn't inject it — the Axiom defaults stand. Never throws.
 */

const DEFAULTS = Object.freeze({
  productName: 'Axiom',
  tagline: 'Sign in to continue',
  logoSvg: null,
  accent: null,
  footer: 'Protected by Axiom',
});

const str = (v, fallback) => (typeof v === 'string' ? v : fallback);
const nonEmptyStr = (v, fallback) => (typeof v === 'string' && v ? v : fallback);

/**
 * The resolved brand: `{ productName, tagline, logoSvg, accent, footer }`, each
 * field defaulted safely. `tagline`/`footer` keep an explicit "" (lets a consumer
 * hide them); `productName`/`logoSvg`/`accent` fall back when empty.
 */
export function getBrand() {
  let raw = null;
  try {
    raw = typeof window !== 'undefined' ? window.__AXIOM_GATE_BRAND__ : null;
  } catch {
    raw = null;
  }
  if (!raw || typeof raw !== 'object') return { ...DEFAULTS };
  return {
    productName: nonEmptyStr(raw.productName, DEFAULTS.productName),
    tagline: str(raw.tagline, DEFAULTS.tagline),
    logoSvg: nonEmptyStr(raw.logoSvg, DEFAULTS.logoSvg),
    accent: nonEmptyStr(raw.accent, DEFAULTS.accent),
    footer: str(raw.footer, DEFAULTS.footer),
  };
}

/**
 * Optional accent override. If `accent` is a valid CSS color, pin it on
 * :root so the whole theme (light + dark) tracks it; otherwise leave the CSS
 * burnt-orange default alone. Never breaks boot.
 */
export function applyBrandAccent(accent) {
  if (!accent || typeof accent !== 'string') return;
  try {
    if (typeof window !== 'undefined' && window.CSS && typeof window.CSS.supports === 'function') {
      if (!window.CSS.supports('color', accent)) return;
    }
    const root = document.documentElement;
    root.style.setProperty('--theme-accent', accent);
    root.style.setProperty('--theme-accent-strong', accent);
  } catch {
    /* branding must never break sign-in */
  }
}

export default { getBrand, applyBrandAccent };
