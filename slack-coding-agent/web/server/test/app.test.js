import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { after, beforeEach, describe, test } from 'node:test';
import { createApp } from '../src/app.js';
import { loadConfig } from '../src/config.js';
import { Store } from '../src/store.js';

const PORT = 8765;
const HOST = `localhost:${PORT}`;

// ------------------------------------------------------------------ fakes

function fakeGitHub() {
  const gh = {
    push: true, archived: false, files: new Map(), branches: new Map([['main', 'sha-main']]),
    prs: [], revoked: [], users: { ghu_alice: 'alice', ghu_bob: 'bob' }, nextToken: 'ghu_alice',
    description: 'A demo',
  };
  gh.repo = () => ({
    name: 'proj', full_name: 'octo/proj', owner: { login: 'octo' }, default_branch: 'main',
    private: false, archived: gh.archived, description: gh.description, language: 'JavaScript',
    permissions: { push: gh.push },
  });
  const json = (status, body) => new Response(body === undefined ? null : JSON.stringify(body), { status });
  gh.fetch = async (input, init = {}) => {
    const url = new URL(String(input));
    const method = init.method ?? 'GET';
    const body = typeof init.body === 'string' ? JSON.parse(init.body) : null;
    const p = url.pathname;
    if (url.host === 'github.com' && p === '/login/oauth/access_token') {
      return json(200, { access_token: gh.nextToken, refresh_token: 'ghr_x', expires_in: 28800 });
    }
    if (p.startsWith('/applications/')) { gh.revoked.push(body.access_token); return json(204); }
    const token = (init.headers?.Authorization ?? '').replace('Bearer ', '');
    if (!gh.users[token]) return json(401, { message: 'Bad credentials' });
    if (p === '/user') return json(200, { login: gh.users[token] });
    if (p === '/user/installations') return json(200, { installations: [{ id: 1 }] });
    if (p === '/user/installations/1/repositories') return json(200, { repositories: [gh.repo()] });
    if (p === '/repos/octo/proj') return json(200, gh.repo());
    if (p === '/repos/octo/proj/contents/') return json(200, [{ name: 'app.js', type: 'file' }, { name: 'src', type: 'dir' }]);
    if (p === '/repos/octo/proj/contents/README.md') {
      const branch = url.searchParams.get('ref') ?? body?.branch;
      const key = `${branch}:README.md`;
      if (method === 'GET') {
        if (!gh.files.has(key)) return json(404, { message: 'Not Found' });
        const text = gh.files.get(key);
        return json(200, { type: 'file', sha: `blob-${text.length}-${text.charCodeAt(2)}`, encoding: 'base64',
          content: Buffer.from(text).toString('base64') });
      }
      if (method === 'PUT') { gh.files.set(key, Buffer.from(body.content, 'base64').toString()); return json(201, {}); }
      if (method === 'DELETE') { gh.files.delete(key); return json(200, {}); }
    }
    if (p === '/repos/octo/proj/git/ref/heads/main') return json(200, { object: { sha: gh.branches.get('main') } });
    if (p === '/repos/octo/proj/git/refs' && method === 'POST') {
      const branch = body.ref.replace('refs/heads/', '');
      gh.branches.set(branch, body.sha);
      for (const [k, v] of [...gh.files]) if (k.startsWith('main:')) gh.files.set(k.replace('main:', `${branch}:`), v);
      return json(201, {});
    }
    if (p.startsWith('/repos/octo/proj/git/refs/heads/') && method === 'DELETE') {
      gh.branches.delete(p.split('/').pop()); return json(204);
    }
    if (p === '/repos/octo/proj/pulls') {
      gh.prs.push(body); return json(201, { html_url: `https://github.com/octo/proj/pull/${gh.prs.length}` });
    }
    return json(404, { message: `unhandled ${method} ${p}` });
  };
  return gh;
}

const fakeAgent = {
  draftReadme: async (facts) => `# ${facts.name}\n\n${facts.description}\n\n${facts.files.map((f) => `- ${f}`).join('\n')}\n`,
  checkReadme: async (content) => {
    const problems = [];
    if (!content.trim()) problems.push("the README can't be empty");
    if (/AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}/.test(content)) problems.push('it appears to add a secret');
    return problems;
  },
};

// ------------------------------------------------------------------ http client

function client(port) {
  const jar = {};
  const request = (method, url, { body, headers = {} } = {}) => new Promise((resolve, reject) => {
    const cookie = Object.entries(jar).map(([k, v]) => `${k}=${v}`).join('; ');
    const data = body === undefined ? undefined : JSON.stringify(body);
    const req = http.request({
      host: '127.0.0.1', port, path: url, method,
      headers: { Host: HOST, ...(cookie ? { Cookie: cookie } : {}),
        ...(data ? { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) } : {}), ...headers },
    }, (res) => {
      for (const c of res.headers['set-cookie'] ?? []) {
        const [pair] = c.split(';');
        const [k, v] = pair.split('=');
        if (/Expires=Thu, 01 Jan 1970/.test(c)) delete jar[k]; else jar[k] = v;
      }
      let text = '';
      res.on('data', (chunk) => { text += chunk; });
      res.on('end', () => {
        let json = null;
        try { json = JSON.parse(text); } catch { /* not json */ }
        resolve({ status: res.statusCode, headers: res.headers, text, json });
      });
    });
    req.on('error', reject);
    if (data) req.write(data);
    req.end();
  });
  const c = { jar, request };
  c.csrf = async () => (await request('GET', '/api/session')).json.csrf;
  c.post = async (url, body = {}, headers = {}) => request('POST', url, { body, headers: { 'X-CSRF-Token': await c.csrf(), ...headers } });
  c.signIn = async (gh, token = 'ghu_alice') => {
    gh.nextToken = token;
    const login = await request('GET', '/auth/login');
    const state = new URL(login.headers.location).searchParams.get('state');
    return request('GET', `/auth/callback?code=abc&state=${state}`);
  };
  return c;
}

// ------------------------------------------------------------------ setup

let dir, config, gh, server, port, store;
const servers = [];

async function start() {
  const s = createApp({ config, store, agent: fakeAgent, fetchImpl: gh.fetch }).listen(0, '127.0.0.1');
  await new Promise((r) => s.once('listening', r));
  servers.push(s);
  return s.address().port;
}

beforeEach(async () => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'web-test-'));
  fs.writeFileSync(path.join(dir, 'config.json'), JSON.stringify({
    web: { port: PORT, baseUrl: `http://${HOST}`, dataDir: dir, clientDist: path.join(dir, 'nope') },
    github: { clientId: 'Iv1.test', clientSecret: 'shh' },
    agentService: { token: 'x'.repeat(32) },
    security: { killSwitchFile: path.join(dir, 'KILL') },
  }));
  config = loadConfig(path.join(dir, 'config.json'));
  gh = fakeGitHub();
  store = new Store(dir, Buffer.alloc(32, 7));
  port = await start();
  server = client(port);
});

after(() => servers.forEach((s) => s.close()));

async function propose(c, action = 'create_readme', content = '# proj\nHello\n') {
  return c.post('/api/repos/octo/proj/proposals', { action, content });
}

// ------------------------------------------------------------------ tests

describe('auth & plumbing', () => {
  test('security headers and session bootstrap', async () => {
    const res = await server.request('GET', '/api/session');
    assert.equal(res.json.signedIn, false);
    assert.ok(res.json.csrf.length > 30);
    assert.match(res.headers['content-security-policy'], /script-src 'self'/);
    assert.equal(res.headers['x-frame-options'], 'DENY');
    assert.match(res.headers['set-cookie'][0], /HttpOnly/);
  });

  test('unexpected Host header is rejected', async () => {
    const res = await server.request('GET', '/api/session', { headers: { Host: 'evil.example:8765' } });
    assert.equal(res.status, 400);
  });

  test('login redirects to GitHub with state and callback URL', async () => {
    const url = new URL((await server.request('GET', '/auth/login')).headers.location);
    assert.equal(url.origin + url.pathname, 'https://github.com/login/oauth/authorize');
    assert.equal(url.searchParams.get('client_id'), 'Iv1.test');
    assert.equal(url.searchParams.get('redirect_uri'), `http://${HOST}/auth/callback`);
    assert.ok(url.searchParams.get('state').length > 30);
  });

  test('callback with wrong state is refused', async () => {
    await server.request('GET', '/auth/login');
    const res = await server.request('GET', '/auth/callback?code=abc&state=forged');
    assert.equal(res.headers.location, '/?error=state');
    assert.equal((await server.request('GET', '/api/session')).json.signedIn, false);
  });

  test('sign-in rotates the session and stores the token encrypted', async () => {
    await server.request('GET', '/auth/login');
    const before = server.jar.agent_session;
    await server.signIn(gh);
    assert.notEqual(server.jar.agent_session, before);
    const s = (await server.request('GET', '/api/session')).json;
    assert.equal(s.login, 'alice');
    const rows = store.db.prepare('SELECT tokens FROM sessions').all();
    assert.ok(rows.every((r) => !String(r.tokens ?? '').includes('ghu_alice')));
  });

  test('logout revokes the token', async () => {
    await server.signIn(gh);
    await server.post('/api/logout');
    assert.deepEqual(gh.revoked, ['ghu_alice']);
    assert.equal((await server.request('GET', '/api/session')).json.signedIn, false);
  });

  test('mutations need CSRF token, JSON and same origin', async () => {
    await server.signIn(gh);
    const noToken = await server.request('POST', '/api/repos/octo/proj/proposals', { body: { action: 'create_readme', content: 'x' } });
    assert.equal(noToken.status, 403);
    const crossOrigin = await server.post('/api/repos/octo/proj/proposals', { action: 'create_readme', content: 'x' },
      { Origin: 'https://evil.example' });
    assert.equal(crossOrigin.status, 403);
  });

  test('API requires sign-in', async () => {
    assert.equal((await server.request('GET', '/api/repos')).status, 401);
  });
});

describe('README changes', () => {
  test('create flow opens a PR on a new branch, never touching main', async () => {
    await server.signIn(gh);
    const repos = (await server.request('GET', '/api/repos')).json.repos;
    assert.equal(repos[0].fullName, 'octo/proj');
    assert.equal((await server.request('GET', '/api/repos/octo/proj')).json.readme.exists, false);

    const created = await propose(server);
    assert.equal(created.status, 201);
    const preview = (await server.request('GET', `/api/proposals/${created.json.id}`)).json;
    assert.match(preview.diff, /\+# proj/);
    assert.equal(preview.status, 'pending');
    assert.equal(gh.prs.length, 0);

    const approved = await server.post(`/api/proposals/${created.json.id}/approve`);
    assert.equal(approved.json.status, 'done');
    assert.equal(gh.prs.length, 1);
    assert.equal(gh.prs[0].base, 'main');
    assert.equal(gh.files.get(`${gh.prs[0].head}:README.md`), '# proj\nHello\n');
    assert.ok(!gh.files.has('main:README.md'));

    const again = await server.post(`/api/proposals/${created.json.id}/approve`);
    assert.equal(again.status, 400);
    assert.equal(gh.prs.length, 1);

    const events = (await server.request('GET', '/api/activity')).json.events.map((e) => e.event);
    assert.ok(events.includes('proposed') && events.includes('approved'));
  });

  test('delete flow', async () => {
    gh.files.set('main:README.md', '# Old\n');
    await server.signIn(gh);
    assert.equal((await server.request('GET', '/api/repos/octo/proj')).json.readme.exists, true);
    const { id } = (await propose(server, 'delete_readme')).json;
    assert.match((await server.request('GET', `/api/proposals/${id}`)).json.diff, /-# Old/);
    await server.post(`/api/proposals/${id}/approve`);
    assert.ok(!gh.files.has(`${gh.prs[0].head}:README.md`));
    assert.equal(gh.files.get('main:README.md'), '# Old\n');
  });

  test('no write permission is refused', async () => {
    gh.push = false;
    await server.signIn(gh);
    const res = await server.request('GET', '/api/repos/octo/proj');
    assert.equal(res.status, 400);
    assert.match(res.json.error, /write access/);
  });

  test('archived and non-allowlisted repos are refused', async () => {
    await server.signIn(gh);
    gh.archived = true;
    assert.equal((await server.request('GET', '/api/repos/octo/proj')).status, 400);
    gh.archived = false;
    config.security.allowedRepos = ['octo/other'];
    assert.equal((await server.request('GET', '/api/repos/octo/proj')).status, 400);
  });

  test('bad content and unknown actions are refused', async () => {
    await server.signIn(gh);
    const secret = await propose(server, 'create_readme', 'key AKIAABCDEFGHIJKLMNOP');
    assert.equal(secret.status, 400);
    assert.match(secret.json.error, /secret/);
    assert.equal((await propose(server, 'create_readme', '   ')).status, 400);
    assert.equal((await propose(server, 'delete_repo')).status, 400);
    gh.files.set('main:README.md', '# x\n');
    assert.match((await propose(server)).json.error, /already has/);
  });

  test('stale preview is refused and leaves no branch behind', async () => {
    await server.signIn(gh);
    const { id } = (await propose(server)).json;
    gh.files.set('main:README.md', '# Someone else added one\n');
    const res = await server.post(`/api/proposals/${id}/approve`);
    assert.equal(res.status, 400);
    assert.match(res.json.error, /changed since the preview/);
    assert.equal(gh.prs.length, 0);
    assert.deepEqual([...gh.branches.keys()], ['main']);
  });

  test('another user cannot see or approve a proposal', async () => {
    await server.signIn(gh);
    const { id } = (await propose(server)).json;
    const bob = client(port);
    await bob.signIn(gh, 'ghu_bob');
    assert.equal((await bob.request('GET', `/api/proposals/${id}`)).status, 404);
    assert.equal((await bob.post(`/api/proposals/${id}/approve`)).status, 404);
    assert.equal(gh.prs.length, 0);
  });

  test('kill switch blocks apply', async () => {
    await server.signIn(gh);
    const { id } = (await propose(server)).json;
    fs.writeFileSync(config.security.killSwitchFile, '');
    const res = await server.post(`/api/proposals/${id}/approve`);
    assert.match(res.json.error, /paused/);
    assert.equal(gh.prs.length, 0);
  });

  test('hourly limit', async () => {
    config.security.maxChangesPerHour = 1;
    await server.signIn(gh);
    assert.equal((await propose(server)).status, 201);
    assert.match((await propose(server)).json.error, /hourly limit/);
  });

  test('AI draft sends only repo facts to the agent service', async () => {
    let seen;
    const agent = { ...fakeAgent, draftReadme: async (facts) => { seen = facts; return '# drafted'; } };
    const s = createApp({ config, store, agent, fetchImpl: gh.fetch }).listen(0, '127.0.0.1');
    await new Promise((r) => s.once('listening', r));
    servers.push(s);
    const c = client(s.address().port);
    await c.signIn(gh);
    const res = await c.post('/api/repos/octo/proj/draft');
    assert.equal(res.json.markdown, '# drafted');
    assert.deepEqual(Object.keys(seen).sort(), ['description', 'files', 'language', 'name']);
    assert.ok(!JSON.stringify(seen).includes('ghu_'));
  });
});
