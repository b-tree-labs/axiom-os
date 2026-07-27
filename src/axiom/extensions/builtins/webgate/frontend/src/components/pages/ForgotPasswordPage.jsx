import { Link } from 'react-router-dom';
import { getBrand } from '../../lib/brand';
import { AxiomMark, AxiomWordmark } from '../AxiomLogo';

/**
 * Password help. There is no self-service reset endpoint — resets are handled by
 * an administrator — so this page is honest about that and points the user back
 * to sign in. No fake API call. Identity comes from the runtime brand.
 */
const ForgotPasswordPage = () => {
  const brand = getBrand();
  return (
  <div className="min-h-screen flex items-center justify-center bg-theme-page py-12 px-4 sm:px-6 lg:px-8">
    <div className="w-full max-w-sm">
      <div className="bg-theme-surface border border-theme rounded-xl shadow-xl shadow-black/5 px-7 py-9 sm:px-9">
        <div className="flex flex-col items-center text-center mb-6">
          <AxiomMark size={46} className="mb-3.5" logoSvg={brand.logoSvg} />
          <AxiomWordmark productName={brand.productName} className="text-theme-primary" />
          <p className="mt-1 text-sm text-theme-muted">Password help</p>
        </div>

        <p className="text-sm text-theme-secondary text-center leading-relaxed">
          Password resets are handled by your administrator — contact them to restore access.
        </p>

        <p className="mt-7 text-center text-sm">
          <Link
            to="/login"
            className="font-medium text-theme-accent-text hover:opacity-80 transition-opacity"
          >
            ← Back to sign in
          </Link>
        </p>
      </div>
    </div>
  </div>
  );
};

export default ForgotPasswordPage;
