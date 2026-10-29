import { useState } from 'react';
import { Link } from 'react-router-dom';
import { login as apiLogin } from '../../lib/authClient';
import { getSafeNextFromSearch } from '../../utils/deeplinkUtils';
import { getBrand } from '../../lib/brand';
import { AxiomMark, AxiomWordmark } from '../AxiomLogo';

/**
 * Sign in — Axiom's default login card. Cookie-session only: on success the gate
 * has set the session cookie, so we hard-navigate to the sanitized `?next=`
 * (default `/`) and the real app picks the session up.
 *
 * Product identity is read at runtime from the server-injected brand (see
 * `lib/brand.js`), so ONE built bundle skins per consumer with no rebuild.
 */
const ProviderMark = ({ kind }) => {
  if (kind === 'google') {
    return (
      <svg viewBox="0 0 48 48" className="w-[18px] h-[18px]" aria-hidden="true">
        <path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9.1 3.6l6.8-6.8C35.7 2.4 30.2 0 24 0 14.6 0 6.5 5.4 2.6 13.2l7.9 6.2C12.4 13.4 17.7 9.5 24 9.5z" />
        <path fill="#4285F4" d="M46.5 24.5c0-1.6-.1-3.1-.4-4.5H24v9h12.7c-.6 3-2.3 5.5-4.8 7.2l7.7 6c4.5-4.2 6.9-10.4 6.9-17.7z" />
        <path fill="#FBBC05" d="M10.5 28.6a14.5 14.5 0 0 1 0-9.2l-7.9-6.2a24 24 0 0 0 0 21.6l7.9-6.2z" />
        <path fill="#34A853" d="M24 48c6.2 0 11.4-2 15.2-5.6l-7.7-6c-2.1 1.4-4.8 2.3-7.5 2.3-6.3 0-11.6-3.9-13.5-9.4l-7.9 6.2C6.5 42.6 14.6 48 24 48z" />
      </svg>
    );
  }
  if (kind === 'entra') {
    return (
      <svg viewBox="0 0 21 21" className="w-[18px] h-[18px]" aria-hidden="true">
        <rect x="0" y="0" width="10" height="10" fill="#F25022" />
        <rect x="11" y="0" width="10" height="10" fill="#7FBA00" />
        <rect x="0" y="11" width="10" height="10" fill="#00A4EF" />
        <rect x="11" y="11" width="10" height="10" fill="#FFB900" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 24 24" className="w-[18px] h-[18px]" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="10" width="18" height="11" rx="2" />
      <path d="M7 10V7a5 5 0 0 1 10 0v3" />
    </svg>
  );
};

const firstTimeHint = (brand) => {
  if (brand.loginHint) return brand.loginHint;
  if (brand.signupUrl) return null; // the signup link renders instead
  const jit = (brand.providers || []).find((p) => p.jit);
  if (jit) return `First time here? Use ${jit.label} and your account is created automatically.`;
  return 'Need an account? Ask your administrator for an invite.';
};

const LoginPage = () => {
  const brand = getBrand();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [remember, setRemember] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    if (!email?.trim()) {
      setError('Email is required');
      return;
    }
    if (!password) {
      setError('Password is required');
      return;
    }
    setLoading(true);
    try {
      await apiLogin({ email: email.trim(), password, remember });
      // Session cookie is now set — hard-navigate so it carries into the app.
      const next = getSafeNextFromSearch(window.location.search) || '/';
      window.location.assign(next);
    } catch (err) {
      setError(err?.message || 'Sign in failed');
      setLoading(false);
    }
  };

  const inputClass =
    'appearance-none block w-full px-3.5 border border-theme-input-border bg-theme-input-bg text-theme-primary placeholder-theme-input-placeholder rounded-lg focus:outline-none text-[15px] h-11';

  return (
    <div className="min-h-screen flex items-center justify-center bg-theme-page py-12 px-4 sm:px-6 lg:px-8">
      <div className="w-full max-w-sm">
        <div className="bg-theme-surface border border-theme rounded-xl shadow-xl shadow-black/5 px-7 py-9 sm:px-9">
          <div className="flex flex-col items-center text-center mb-7">
            <AxiomMark size={46} className="mb-3.5" logoSvg={brand.logoSvg} />
            <AxiomWordmark productName={brand.productName} className="text-theme-primary" />
            {brand.tagline && <p className="mt-1 text-sm text-theme-muted">{brand.tagline}</p>}
          </div>

          {(brand.providers?.length ? brand.providers : brand.sso ? [{ ...brand.sso, name: 'sso', kind: 'oidc' }] : []).length > 0 && (
            <>
              <div className="flex flex-col gap-2.5" data-testid="login-sso">
                {(brand.providers?.length ? brand.providers : [{ ...brand.sso, name: 'sso', kind: 'oidc' }]).map((p) => (
                  <a
                    key={p.name}
                    href={`${p.url}?next=${encodeURIComponent(
                      getSafeNextFromSearch(window.location.search) || '/'
                    )}`}
                    data-testid={`login-sso-${p.name}`}
                    className="btn btn-md w-full flex items-center justify-center gap-2.5 border border-theme-input-border bg-theme-input-bg text-theme-primary hover:border-theme-accent transition-colors"
                  >
                    <ProviderMark kind={p.kind} />
                    {p.label}
                  </a>
                ))}
              </div>
              <div className="my-5 flex items-center gap-3 text-xs text-theme-muted">
                <span className="h-px flex-1 bg-theme-input-border" />
                or sign in with a password
                <span className="h-px flex-1 bg-theme-input-border" />
              </div>
            </>
          )}

          <form onSubmit={handleSubmit} noValidate>
            {error && (
              <div
                role="alert"
                data-testid="login-error"
                className="mb-4 text-sm text-status-error bg-status-error/5 border border-status-error/20 px-3 py-2.5 rounded-lg"
              >
                {error}
              </div>
            )}

            <div className="space-y-4">
              <div>
                <label htmlFor="email" className="block text-xs font-medium text-theme-secondary mb-1.5">
                  Email
                </label>
                <input
                  id="email"
                  name="email"
                  type="email"
                  autoComplete="username"
                  autoFocus
                  required
                  value={email}
                  onChange={(e) => {
                    setEmail(e.target.value);
                    setError('');
                  }}
                  data-testid="login-email"
                  className={inputClass}
                  placeholder="you@example.org"
                />
              </div>

              <div>
                <div className="flex items-center justify-between mb-1.5">
                  <label htmlFor="password" className="block text-xs font-medium text-theme-secondary">
                    Password
                  </label>
                  <Link
                    to="/forgot"
                    className="text-xs font-medium text-theme-accent-text hover:opacity-80 transition-opacity"
                  >
                    Forgot password?
                  </Link>
                </div>
                <div className="relative flex items-center">
                  <input
                    id="password"
                    name="password"
                    type={showPassword ? 'text' : 'password'}
                    autoComplete="current-password"
                    required
                    value={password}
                    onChange={(e) => {
                      setPassword(e.target.value);
                      setError('');
                    }}
                    data-testid="login-password"
                    className={`${inputClass} pr-16`}
                    placeholder="Your password"
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword((v) => !v)}
                    className="absolute right-1.5 h-8 px-2.5 rounded-md text-xs font-semibold text-theme-muted hover:text-theme-primary hover:bg-theme-nav-hover transition-colors"
                    aria-label={showPassword ? 'Hide password' : 'Show password'}
                    data-testid="login-password-toggle"
                  >
                    {showPassword ? 'Hide' : 'Show'}
                  </button>
                </div>
              </div>

              <label className="flex items-center gap-2.5 text-sm text-theme-secondary cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={remember}
                  onChange={(e) => setRemember(e.target.checked)}
                  data-testid="login-remember"
                  className="h-4 w-4 rounded border-theme-input-border text-theme-accent focus:ring-0 focus:ring-offset-0"
                  style={{ accentColor: 'var(--theme-accent)' }}
                />
                Remember me
              </label>
            </div>

            <button
              type="submit"
              disabled={loading}
              data-testid="login-submit"
              className="btn btn-md btn-primary mt-6 w-full"
            >
              {loading ? 'Signing in…' : 'Sign in'}
            </button>
          </form>
        </div>

        {firstTimeHint(brand) && (
            <p className="mt-4 text-center text-[13px] text-theme-muted">{firstTimeHint(brand)}</p>
          )}
          {brand.signupUrl && (
            <p className="mt-4 text-center text-[13px] text-theme-muted">New here? <a className="text-theme-accent" href={brand.signupUrl}>Create an account</a></p>
          )}
          {brand.footer && (
          <p className="mt-6 text-center text-xs text-theme-muted">{brand.footer}</p>
        )}
      </div>
    </div>
  );
};

export default LoginPage;
