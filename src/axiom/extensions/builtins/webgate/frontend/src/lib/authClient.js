/**
 * Auth client for the webgate JSON seam.
 *
 * Cookie-session ONLY — the gate sets an httpOnly ES256 session cookie, so there
 * are no tokens here: nothing is read from or written to localStorage, and no
 * Authorization header is ever sent. Every call uses `credentials: 'include'`
 * so the browser carries the session cookie.
 *
 * Backend routes (see webgate/api/routers.py):
 *   POST /gate/session  {email,password,remember} → 200 user JSON | 401 {detail}
 *   GET  /gate/me                                  → 200 user JSON | 401
 *   POST /gate/logout                              → clears the session cookie
 *
 * User JSON shape: { sub, email, name, roles, role }.
 */

const JSON_HEADERS = { 'Content-Type': 'application/json' };

/**
 * Authenticate. On success resolves to the user JSON; on 401 throws an Error
 * whose message is the server's `detail` (e.g. "Incorrect email or password").
 */
export async function login({ email, password, remember = false }) {
  const res = await fetch('/gate/session', {
    method: 'POST',
    credentials: 'include',
    headers: JSON_HEADERS,
    body: JSON.stringify({ email, password, remember }),
  });

  if (res.ok) {
    return res.json();
  }

  let detail = 'Incorrect email or password';
  try {
    const body = await res.json();
    if (body?.detail) detail = body.detail;
  } catch {
    /* non-JSON error body — keep the default message */
  }
  throw new Error(detail);
}

/** "Am I logged in?" — resolves to the user JSON on 200, or null on 401. */
export async function me() {
  const res = await fetch('/gate/me', { credentials: 'include' });
  if (res.ok) {
    return res.json();
  }
  return null;
}

/** Clear the session cookie. */
export async function logout() {
  await fetch('/gate/logout', { method: 'POST', credentials: 'include' });
}

export default { login, me, logout };
