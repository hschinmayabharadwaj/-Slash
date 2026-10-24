// Thin client for the Node server's JSON API. Every mutation sends the
// per-session CSRF token; the session cookie is HttpOnly.
let csrf = '';

export class ApiError extends Error {
  constructor(message, status, signedOut = false) {
    super(message);
    this.status = status;
    this.signedOut = signedOut;
  }
}

async function request(method, url, body) {
  const res = await fetch(url, {
    method,
    credentials: 'same-origin',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(data.error || `Request failed (${res.status})`, res.status, Boolean(data.signedOut));
  return data;
}

export async function getSession() {
  const s = await request('GET', '/api/session');
  csrf = s.csrf;
  return s;
}

export const api = {
  repos: () => request('GET', '/api/repos'),
  repo: (owner, name) => request('GET', `/api/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}`),
  draft: (owner, name) => request('POST', `/api/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/draft`, {}),
  propose: (owner, name, action, content) =>
    request('POST', `/api/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/proposals`, { action, content }),
  proposal: (id) => request('GET', `/api/proposals/${encodeURIComponent(id)}`),
  approve: (id) => request('POST', `/api/proposals/${encodeURIComponent(id)}/approve`, {}),
  discard: (id) => request('POST', `/api/proposals/${encodeURIComponent(id)}/discard`, {}),
  activity: () => request('GET', '/api/activity'),
  logout: () => request('POST', '/api/logout', {}),
};
