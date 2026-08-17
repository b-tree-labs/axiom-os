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

        {brand.footer && (
          <p className="mt-6 text-center text-xs text-theme-muted">{brand.footer}</p>
        )}
      </div>
    </div>
  );
};

export default LoginPage;
