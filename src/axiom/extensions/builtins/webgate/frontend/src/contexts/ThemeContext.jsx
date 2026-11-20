/**
 * ThemeContext — system-aware theme with localStorage persistence.
 *
 * Choices (THEME_OPTIONS):
 *   system: follow the OS appearance, shifting live when it toggles (default)
 *   light:  stark neutral, white cards
 *   dark:   deep slate
 *
 * `theme` is the user's CHOICE (may be 'system'); `resolvedTheme` is what's
 * actually applied to `data-theme` (always light|dark). When the choice is
 * 'system' we resolve via `prefers-color-scheme` and subscribe to its change
 * event, so the UI shifts with macOS/Windows light↔dark like any other app.
 */

import { createContext, useContext, useState, useEffect } from 'react';

const STORAGE_KEY = 'axiom_theme';

export const THEME_OPTIONS = [
  { value: 'system', label: 'System', description: 'Match your device, shifts with OS light/dark' },
  { value: 'light', label: 'Light', description: 'Neutral light' },
  { value: 'dark', label: 'Dark', description: 'Deep slate' },
];

const VALID_THEMES = new Set(THEME_OPTIONS.map((o) => o.value));

const prefersDark = () =>
  typeof window !== 'undefined' && Boolean(window.matchMedia?.('(prefers-color-scheme: dark)').matches);

/** A user choice → the concrete theme to apply (light|dark). */
const resolveTheme = (choice) => (choice === 'system' ? (prefersDark() ? 'dark' : 'light') : choice);

const getInitialChoice = () => {
  if (typeof window === 'undefined') return 'system';
  const stored = localStorage.getItem(STORAGE_KEY);
  return VALID_THEMES.has(stored) ? stored : 'system';
};

const ThemeContext = createContext();

export const useTheme = () => {
  const ctx = useContext(ThemeContext);
  if (!ctx) {
    throw new Error('useTheme must be used within ThemeProvider');
  }
  return ctx;
};

export const ThemeProvider = ({ children }) => {
  // The user's choice (light | dark | system).
  const [theme, setThemeState] = useState(getInitialChoice);
  // The concrete theme applied to data-theme. Set synchronously on first render
  // to avoid a flash of the wrong theme.
  const [resolvedTheme, setResolvedTheme] = useState(() => {
    const r = resolveTheme(getInitialChoice());
    if (typeof document !== 'undefined') {
      document.documentElement.setAttribute('data-theme', r);
    }
    return r;
  });

  // Apply + persist whenever the choice changes.
  useEffect(() => {
    const r = resolveTheme(theme);
    setResolvedTheme(r);
    document.documentElement.setAttribute('data-theme', r);
    localStorage.setItem(STORAGE_KEY, theme);
  }, [theme]);

  // While on 'system', follow live OS light↔dark transitions.
  useEffect(() => {
    if (theme !== 'system' || typeof window === 'undefined' || !window.matchMedia) return undefined;
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const onChange = (e) => {
      const r = e.matches ? 'dark' : 'light';
      setResolvedTheme(r);
      document.documentElement.setAttribute('data-theme', r);
    };
    // Safari <14 used addListener/removeListener; modern browsers use add/removeEventListener.
    if (mq.addEventListener) mq.addEventListener('change', onChange);
    else if (mq.addListener) mq.addListener(onChange);
    return () => {
      if (mq.removeEventListener) mq.removeEventListener('change', onChange);
      else if (mq.removeListener) mq.removeListener(onChange);
    };
  }, [theme]);

  const setTheme = (next) => {
    setThemeState(VALID_THEMES.has(next) ? next : 'system');
  };

  /** Toggle light ↔ dark explicitly (opts out of 'system'). */
  const toggleTheme = () => {
    setThemeState(resolvedTheme === 'dark' ? 'light' : 'dark');
  };

  const value = {
    theme, // the choice (may be 'system')
    resolvedTheme, // what's actually applied (light|dark)
    setTheme,
    toggleTheme,
    isDark: resolvedTheme === 'dark',
    isLight: resolvedTheme === 'light',
  };

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
};
