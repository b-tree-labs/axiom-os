// Brand-neutral `next` sanitizer — lifted as-is from the portable component set.
// Only ever permits a local, single-slash path so a `?next=` value can never be
// turned into an open redirect (no absolute or protocol-relative URLs).

const MAX_NEXT_LENGTH = 2000;

export function sanitizeRelativeUrl(candidate) {
  if (typeof candidate !== 'string') return null;

  const trimmed = candidate.trim();
  if (!trimmed || trimmed.length > MAX_NEXT_LENGTH) return null;
  if (!trimmed.startsWith('/')) return null;
  if (trimmed.startsWith('//')) return null;

  return trimmed;
}

export function getSafeNextFromSearch(search = '') {
  const params = new URLSearchParams(search);
  return sanitizeRelativeUrl(params.get('next'));
}
