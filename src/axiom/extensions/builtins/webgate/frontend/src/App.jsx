import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import { ThemeProvider } from './contexts/ThemeContext';
import ThemeToggle from './components/ThemeToggle';
import LoginPage from './components/pages/LoginPage';
import ForgotPasswordPage from './components/pages/ForgotPasswordPage';

/**
 * Axiom's default web auth UI. Two client routes only — the login and the
 * (admin-managed) password-help page. On a successful login the LoginPage reads
 * a sanitized `?next=` and hard-navigates there so the freshly set gate cookie
 * carries into the real app.
 */
function App() {
  return (
    <Router basename="/gate" future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <ThemeProvider>
        {/* Theme toggle, pinned to a corner and above the card. */}
        <div className="fixed top-4 right-4 z-10">
          <ThemeToggle />
        </div>
        <Routes>
          <Route path="/" element={<LoginPage />} />
          <Route path="/login" element={<LoginPage />} />
          <Route path="/forgot" element={<ForgotPasswordPage />} />
          <Route path="*" element={<Navigate to="/login" replace />} />
        </Routes>
      </ThemeProvider>
    </Router>
  );
}

export default App;
