// Thin client for the FastAPI server's JSON API. Every mutation sends the
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

const repoPath = (owner, name) => `/api/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}`;
const taskPath = (id) => `/api/tasks/${encodeURIComponent(id)}`;

export const api = {
  me: () => request('GET', '/api/me'),
  repos: () => request('GET', '/api/repos'),
  repo: (owner, name) => request('GET', repoPath(owner, name)),
  indexStatus: (owner, name) => request('GET', `${repoPath(owner, name)}/index`),
  reindex: (owner, name) => request('POST', `${repoPath(owner, name)}/index`, {}),
  ask: (owner, name, question) => request('POST', `${repoPath(owner, name)}/ask`, { question }),
  createTask: (owner, name, req, acceptanceCriteria) =>
    request('POST', `${repoPath(owner, name)}/tasks`, { request: req, acceptanceCriteria }),
  tasks: () => request('GET', '/api/tasks'),
  task: (id) => request('GET', taskPath(id)),
  decidePlan: (id, approve) => request('POST', `${taskPath(id)}/plan`, { approve }),
  decideChanges: (id, approve) => request('POST', `${taskPath(id)}/changes`, { approve }),
  clarify: (id, answers) => request('POST', `${taskPath(id)}/clarify`, { answers }),
  adminAudit: () => request('GET', '/api/admin/audit'),
  killSwitch: (on) => request('POST', '/api/admin/kill-switch', { on }),
  draft: (owner, name) => request('POST', `/api/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/draft`, {}),
  propose: (owner, name, action, content) =>
    request('POST', `/api/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/proposals`, { action, content }),
  proposal: (id) => request('GET', `/api/proposals/${encodeURIComponent(id)}`),
  approve: (id) => request('POST', `/api/proposals/${encodeURIComponent(id)}/approve`, {}),
  discard: (id) => request('POST', `/api/proposals/${encodeURIComponent(id)}/discard`, {}),
  activity: () => request('GET', '/api/activity'),
  logout: () => request('POST', '/api/logout', {}),
};
