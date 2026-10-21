/**
 * AxiomLogo — the brand-neutral default lockup for Axiom's login UI.
 *
 * This is the SINGLE swap point for product identity: pass `productName` to
 * rebrand the wordmark, or drop in a different `AxiomMark` glyph. No agriculture
 * or third-party imagery — a minimal rounded-square mark in the accent color
 * (which follows the theme via `--theme-accent`) plus a clean wordmark.
 */

/**
 * The square Axiom mark — a rounded square in the accent color with a minimal
 * white apex glyph. Fill uses the theme accent var so it tracks light/dark.
 *
 * `logoSvg` overrides the default glyph with trusted server config (the brand's
 * inline SVG or emoji, from `LoginBrand.logo`). It is injected verbatim into a
 * span sized like the mark — only ever set from server-controlled brand config.
 */
export const AxiomMark = ({ size = 40, className = '', logoSvg = null }) => {
  if (logoSvg) {
    return (
      <span
        className={`inline-flex items-center justify-center shrink-0 ${className}`}
        style={{ width: size, height: size }}
        role="img"
        aria-label="logo"
        // eslint-disable-next-line react/no-danger -- trusted server brand config
        dangerouslySetInnerHTML={{ __html: logoSvg }}
      />
    );
  }
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      className={className}
      role="img"
      aria-label="Axiom"
      focusable="false"
    >
      <rect x="2" y="2" width="60" height="60" rx="16" fill="var(--theme-accent, #bf5700)" />
      <path
        d="M20 43 L32 20 L44 43"
        fill="none"
        stroke="#fff"
        strokeWidth="5.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path d="M26 35 H38" fill="none" stroke="#fff" strokeWidth="5.5" strokeLinecap="round" />
    </svg>
  );
};

/** The product wordmark — renders `productName` (default "Axiom"). */
export const AxiomWordmark = ({ productName = 'Axiom', className = '' }) => (
  <span className={`text-app-brand ${className}`}>{productName}</span>
);

/**
 * Full lockup: mark + wordmark. Swap `productName` (and optionally the mark) to
 * rebrand the whole auth UI from one place.
 *
 * @param {string}  productName   wordmark text (default "Axiom")
 * @param {boolean} showWordmark  render the wordmark beside the mark (default true)
 * @param {number}  markSize      px size of the square mark (default 40)
 * @param {string}  className     extra classes on the root container
 * @param {string}  wordmarkClassName  extra classes on the wordmark (sizing/color)
 */
const AxiomLogo = ({
  productName = 'Axiom',
  showWordmark = true,
  markSize = 40,
  className = '',
  wordmarkClassName = 'text-theme-primary',
}) => (
  <span className={`inline-flex items-center gap-2.5 ${className}`}>
    <AxiomMark size={markSize} className="shrink-0" />
    {showWordmark && <AxiomWordmark productName={productName} className={wordmarkClassName} />}
  </span>
);

export default AxiomLogo;
