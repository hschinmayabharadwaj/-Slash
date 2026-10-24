// GitHub sign-in (OAuth web flow for a GitHub App) and API calls made as the
// signed-in user. User-to-server tokens can never do more than BOTH the App's
// permissions (Contents + Pull requests) AND the person's own access, only on
// repos where the App is installed, and they expire (8h) with a refresh token.

const GITHUB = 'https://github.com';
const API = 'https://api.github.com';

export class GitHubError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.status = status;
  }
}

const enc = encodeURIComponent;

function tokenSet(data) {
  if (!data?.access_token) {
    throw new GitHubError(`GitHub sign-in failed: ${data?.error_description || data?.error || 'no token'}`);
  }
  const expiresIn = Number(data.expires_in || 0);
  return {
    accessToken: data.access_token,
    refreshToken: data.refresh_token || '',
    expiresAt: expiresIn ? Date.now() + expiresIn * 1000 : 0,
  };
}

export const tokenExpired = (t) => Boolean(t.expiresAt) && Date.now() > t.expiresAt - 120_000;

export class GitHubOAuth {
  constructor({ clientId, clientSecret, redirectUri, fetchImpl = fetch }) {
    Object.assign(this, { clientId, clientSecret, redirectUri, fetch: fetchImpl });
  }

  authorizeUrl(state) {
    const q = new URLSearchParams({
      client_id: this.clientId, redirect_uri: this.redirectUri, state, allow_signup: 'false',
    });
    return `${GITHUB}/login/oauth/authorize?${q}`;
  }

  async #token(extra) {
    const res = await this.fetch(`${GITHUB}/login/oauth/access_token`, {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ client_id: this.clientId, client_secret: this.clientSecret, ...extra }),
    });
    if (!res.ok) throw new GitHubError('GitHub sign-in failed.', res.status);
    return tokenSet(await res.json());
  }

  exchangeCode(code) {
    return this.#token({ code, redirect_uri: this.redirectUri });
  }

  refresh(refreshToken) {
    return this.#token({ grant_type: 'refresh_token', refresh_token: refreshToken });
  }

  /** Invalidate the token at GitHub (best effort, on sign-out). */
  async revoke(accessToken) {
    try {
      const basic = Buffer.from(`${this.clientId}:${this.clientSecret}`).toString('base64');
      await this.fetch(`${API}/applications/${enc(this.clientId)}/token`, {
        method: 'DELETE',
        headers: { Authorization: `Basic ${basic}`, Accept: 'application/vnd.github+json', 'Content-Type': 'application/json' },
        body: JSON.stringify({ access_token: accessToken }),
      });
    } catch { /* best effort */ }
  }
}

export class GitHubUser {
  constructor(token, fetchImpl = fetch) {
    this.token = token;
    this.fetch = fetchImpl;
  }

  async call(method, path, { body, query, ok404 = false } = {}) {
    const url = new URL(API + path);
    for (const [k, v] of Object.entries(query ?? {})) url.searchParams.set(k, v);
    const res = await this.fetch(url, {
      method,
      headers: {
        Authorization: `Bearer ${this.token}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        ...(body ? { 'Content-Type': 'application/json' } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (res.status === 404 && ok404) return null;
    if (res.status === 401) throw new GitHubError('Your GitHub session expired. Please connect again.', 401);
    if (res.status === 403) throw new GitHubError('GitHub refused this action (no permission or rate limited).', 403);
    if (res.status >= 400) {
      let message = '';
      try { message = String((await res.json()).message ?? '').slice(0, 200); } catch { /* ignore */ }
      throw new GitHubError(`GitHub error ${res.status}: ${message}`, res.status);
    }
    if (res.status === 204) return {};
    const text = await res.text();
    return text ? JSON.parse(text) : {};
  }

  getUser() { return this.call('GET', '/user'); }

  /** Repos where the App is installed AND this user has access. */
  async listRepos(limit = 300) {
    const repos = [];
    const { installations = [] } = await this.call('GET', '/user/installations', { query: { per_page: 100 } });
    for (const inst of installations) {
      for (let page = 1; repos.length < limit; page++) {
        const data = await this.call('GET', `/user/installations/${inst.id}/repositories`,
          { query: { per_page: 100, page } });
        repos.push(...(data.repositories ?? []));
        if ((data.repositories ?? []).length < 100) break;
      }
    }
    return repos.sort((a, b) => a.full_name.localeCompare(b.full_name)).slice(0, limit);
  }

  getRepo(owner, name) { return this.call('GET', `/repos/${enc(owner)}/${enc(name)}`); }

  /** File text + blob sha, or null if it doesn't exist. */
  async getFile(owner, name, filePath, ref) {
    const data = await this.call('GET', `/repos/${enc(owner)}/${enc(name)}/contents/${enc(filePath)}`,
      { query: { ref }, ok404: true });
    if (!data || Array.isArray(data) || data.type !== 'file') return null;
    const text = data.encoding === 'base64' ? Buffer.from(data.content ?? '', 'base64').toString('utf8') : '';
    return { sha: data.sha, text };
  }

  async listRoot(owner, name, ref) {
    const data = (await this.call('GET', `/repos/${enc(owner)}/${enc(name)}/contents/`, { query: { ref } })) ?? [];
    return data.slice(0, 200).map((i) => i.name + (i.type === 'dir' ? '/' : ''));
  }

  async branchSha(owner, name, branch) {
    const data = await this.call('GET', `/repos/${enc(owner)}/${enc(name)}/git/ref/heads/${enc(branch)}`);
    return data.object.sha;
  }

  createBranch(owner, name, branch, sha) {
    return this.call('POST', `/repos/${enc(owner)}/${enc(name)}/git/refs`, { body: { ref: `refs/heads/${branch}`, sha } });
  }

  deleteBranch(owner, name, branch) {
    return this.call('DELETE', `/repos/${enc(owner)}/${enc(name)}/git/refs/heads/${enc(branch)}`, { ok404: true });
  }

  putFile(owner, name, filePath, text, message, branch) {
    return this.call('PUT', `/repos/${enc(owner)}/${enc(name)}/contents/${enc(filePath)}`,
      { body: { message, content: Buffer.from(text, 'utf8').toString('base64'), branch } });
  }

  deleteFile(owner, name, filePath, sha, message, branch) {
    return this.call('DELETE', `/repos/${enc(owner)}/${enc(name)}/contents/${enc(filePath)}`,
      { body: { message, sha, branch } });
  }

  async createPullRequest(owner, name, head, base, title, body) {
    const data = await this.call('POST', `/repos/${enc(owner)}/${enc(name)}/pulls`, { body: { title, head, base, body } });
    return data.html_url;
  }
}
