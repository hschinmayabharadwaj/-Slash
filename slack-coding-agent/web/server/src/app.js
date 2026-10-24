// Express app: GitHub sign-in, JSON API for the React page, static SPA.
//
// Protections on top of GitHub's own permission checks:
//  - binds to loopback and rejects unexpected Host headers (DNS rebinding)
//  - HttpOnly SameSite=Lax session cookie; mutations need the per-session
//    X-CSRF-Token header, a JSON content type and a same-origin Origin
//  - OAuth state checked in constant time; session ID rotated at sign-in
//  - strict CSP, no framing, no-referrer, no caching of API responses
//  - every proposal / approval / refusal goes to the audit log
import fs from 'node:fs';
import path from 'node:path';
import express from 'express';
import { ACTIONS, ActionRefused, apply, authorize, fingerprint, propose, README } from './actions.js';
import { randomToken, safeEqual } from './crypto.js';
import { GitHubError, GitHubOAuth, GitHubUser, tokenExpired } from './github.js';

const COOKIE = 'agent_session';

function parseCookies(header = '') {
  return Object.fromEntries(header.split(';').map((p) => p.trim().split('=')).filter(([k]) => k)
    .map(([k, ...v]) => [k, decodeURIComponent(v.join('='))]));
}

export function createApp({ config, store, agent, fetchImpl = fetch }) {
  const app = express();
  app.disable('x-powered-by');
  app.set('trust proxy', false);

  const base = new URL(config.web.baseUrl);
  const secure = base.protocol === 'https:';
  const allowedHosts = new Set([base.host, `127.0.0.1:${config.web.port}`, `localhost:${config.web.port}`]);
  const oauth = new GitHubOAuth({
    clientId: config.github.clientId,
    clientSecret: config.github.clientSecret,
    redirectUri: `${config.web.baseUrl.replace(/\/+$/, '')}/auth/callback`,
    fetchImpl,
  });

  // ---------------------------------------------------------------- plumbing

  app.use((req, res, next) => {
    if (!allowedHosts.has(req.headers.host)) return res.status(400).send('Bad host');
    res.set({
      'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; "
        + "img-src 'self' data: https://avatars.githubusercontent.com; connect-src 'self'; "
        + "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'",
      'X-Frame-Options': 'DENY',
      'X-Content-Type-Options': 'nosniff',
      'Referrer-Policy': 'no-referrer',
      'Cross-Origin-Opener-Policy': 'same-origin',
    });
    next();
  });
  app.use('/api', (req, res, next) => { res.set('Cache-Control', 'no-store'); next(); });
  app.use(express.json({ limit: '128kb' }));

  const setCookie = (res, id) => res.cookie(COOKIE, id, {
    httpOnly: true, sameSite: 'lax', secure, path: '/', maxAge: config.web.sessionHours * 3600 * 1000,
  });

  const sessionOf = (req) => {
    const id = parseCookies(req.headers.cookie)[COOKIE];
    return { id, session: store.getSession(id) };
  };

  /** Require a signed-in user; attaches req.user = {id, session, login, gh}. Refreshes tokens. */
  const signedIn = async (req, res, next) => {
    try {
      const { id, session } = sessionOf(req);
      if (!session?.login) return res.status(401).json({ error: 'Not connected to GitHub.', signedOut: true });
      let tokens = store.tokens(session);
      if (!tokens) return res.status(401).json({ error: 'Not connected to GitHub.', signedOut: true });
      if (tokenExpired(tokens)) {
        if (!tokens.refreshToken) {
          store.deleteSession(id);
          return res.status(401).json({ error: 'Your GitHub session expired.', signedOut: true });
        }
        tokens = await oauth.refresh(tokens.refreshToken);
        store.updateTokens(id, tokens);
      }
      req.user = { id, session, login: session.login, gh: new GitHubUser(tokens.accessToken, fetchImpl) };
      next();
    } catch (e) {
      next(e);
    }
  };

  const csrf = (req, res, next) => {
    const origin = req.headers.origin;
    if (origin) {
      let host = '';
      try { host = new URL(origin).host; } catch { /* invalid */ }
      if (!allowedHosts.has(host)) return res.status(403).json({ error: 'Cross-origin request refused.' });
    }
    if (!req.is('application/json')) return res.status(415).json({ error: 'Expected JSON.' });
    const { session } = req.user ?? sessionOf(req);
    if (!session || !safeEqual(req.headers['x-csrf-token'], session.csrf)) {
      return res.status(403).json({ error: 'Invalid CSRF token.' });
    }
    next();
  };

  const wrap = (fn) => (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next);
  const log = (login, event, detail) => store.record(login, event, detail);

  // ---------------------------------------------------------------- auth

  app.get('/auth/login', (req, res) => {
    const state = randomToken();
    setCookie(res, store.newSession(state));
    res.redirect(302, oauth.authorizeUrl(state));
  });

  app.get('/auth/callback', wrap(async (req, res) => {
    const { id, session } = sessionOf(req);
    const state = String(req.query.state ?? '');
    if (!session?.oauth_state || !safeEqual(state, session.oauth_state)) {
      return res.redirect(302, '/?error=state');
    }
    if (req.query.error) {
      store.deleteSession(id);
      return res.redirect(302, '/?error=cancelled');
    }
    const tokens = await oauth.exchangeCode(String(req.query.code ?? ''));
    const user = await new GitHubUser(tokens.accessToken, fetchImpl).getUser();
    setCookie(res, store.signIn(id, user.login, tokens));
    log(user.login, 'signed_in', {});
    res.redirect(302, '/');
  }));

  app.get('/api/session', (req, res) => {
    let { id, session } = sessionOf(req);
    if (!session) {
      id = store.newSession();
      session = store.getSession(id);
      setCookie(res, id);
    }
    res.json({ signedIn: Boolean(session.login), login: session.login ?? null, csrf: session.csrf, actions: ACTIONS });
  });

  app.post('/api/logout', csrf, wrap(async (req, res) => {
    const { id, session } = sessionOf(req);
    const tokens = store.tokens(session);
    if (tokens) await oauth.revoke(tokens.accessToken);
    store.deleteSession(id);
    if (session?.login) log(session.login, 'signed_out', {});
    res.clearCookie(COOKIE, { path: '/' }).json({ ok: true });
  }));

  // ---------------------------------------------------------------- repos & changes

  const repoView = (r) => ({
    fullName: r.full_name, owner: r.owner.login, name: r.name, private: r.private,
    defaultBranch: r.default_branch, description: r.description ?? '',
  });

  app.get('/api/repos', signedIn, wrap(async (req, res) => {
    res.json({ repos: (await req.user.gh.listRepos()).map(repoView) });
  }));

  app.get('/api/repos/:owner/:name', signedIn, wrap(async (req, res) => {
    const { owner, name } = req.params;
    const repo = await authorize(req.user.gh, config, owner, name);
    const readme = await req.user.gh.getFile(owner, name, README, repo.default_branch);
    res.json({ repo: repoView(repo), readme: readme ? { exists: true, text: readme.text.slice(0, 20000) } : { exists: false } });
  }));

  app.post('/api/repos/:owner/:name/draft', signedIn, csrf, wrap(async (req, res) => {
    const { owner, name } = req.params;
    const repo = await authorize(req.user.gh, config, owner, name);
    const files = await req.user.gh.listRoot(owner, name, repo.default_branch);
    const markdown = await agent.draftReadme({
      name: repo.name, description: repo.description ?? '', language: repo.language ?? '', files,
    });
    log(req.user.login, 'draft_readme', { repo: repo.full_name });
    res.json({ markdown });
  }));

  app.post('/api/repos/:owner/:name/proposals', signedIn, csrf, wrap(async (req, res) => {
    const { owner, name } = req.params;
    const { login, gh } = req.user;
    const action = String(req.body?.action ?? '');
    if (store.proposalsLastHour(login) >= config.security.maxChangesPerHour) {
      throw new ActionRefused("You've hit the hourly limit for changes. Try again later.");
    }
    let proposal;
    try {
      proposal = await propose(gh, agent, config, owner, name, action, String(req.body?.content ?? ''));
    } catch (e) {
      if (e instanceof ActionRefused) log(login, 'refused', { repo: `${owner}/${name}`, action, reason: e.message });
      throw e;
    }
    const fp = fingerprint(proposal);
    const id = store.saveProposal(login, proposal, fp);
    log(login, 'proposed', { repo: `${owner}/${name}`, action, fingerprint: fp });
    res.status(201).json({ id });
  }));

  const proposalView = (rec) => ({
    id: rec.id, status: rec.status, prUrl: rec.pr_url,
    action: rec.proposal.action, actionLabel: ACTIONS[rec.proposal.action],
    owner: rec.proposal.owner, name: rec.proposal.name,
    fullName: `${rec.proposal.owner}/${rec.proposal.name}`,
    baseBranch: rec.proposal.baseBranch, diff: rec.proposal.diff,
  });

  app.get('/api/proposals/:id', signedIn, (req, res) => {
    const rec = store.getProposal(req.params.id, req.user.login);
    if (!rec) return res.status(404).json({ error: 'Not found.' });
    res.json(proposalView(rec));
  });

  app.post('/api/proposals/:id/approve', signedIn, csrf, wrap(async (req, res) => {
    const { login, gh } = req.user;
    const rec = store.getProposal(req.params.id, login);
    if (!rec) return res.status(404).json({ error: 'Not found.' });
    if (fingerprint(rec.proposal) !== rec.fingerprint) {
      store.finishProposal(rec.id, 'rejected');
      throw new ActionRefused('This change no longer matches its preview.');
    }
    if (!store.claimProposal(rec.id, login)) {
      throw new ActionRefused('This change was already handled or has expired. Start a new one.');
    }
    try {
      const prUrl = await apply(gh, config, rec.proposal, login);
      store.finishProposal(rec.id, 'done', prUrl);
      log(login, 'approved', { repo: proposalView(rec).fullName, action: rec.proposal.action, fingerprint: rec.fingerprint, pr: prUrl });
      res.json({ status: 'done', prUrl });
    } catch (e) {
      store.finishProposal(rec.id, 'failed');
      log(login, 'apply_failed', { repo: proposalView(rec).fullName, action: rec.proposal.action, error: e.message.slice(0, 200) });
      throw e;
    }
  }));

  app.post('/api/proposals/:id/discard', signedIn, csrf, (req, res) => {
    if (store.claimProposal(req.params.id, req.user.login)) {
      store.finishProposal(req.params.id, 'discarded');
      log(req.user.login, 'discarded', { proposal: req.params.id });
    }
    res.json({ ok: true });
  });

  app.get('/api/activity', signedIn, (req, res) => {
    res.json({ events: store.events(req.user.login) });
  });

  app.use('/api', (req, res) => res.status(404).json({ error: 'Not found.' }));

  // ---------------------------------------------------------------- React SPA

  const dist = path.resolve(config.web.clientDist);
  if (fs.existsSync(path.join(dist, 'index.html'))) {
    app.use(express.static(dist, { index: false, maxAge: '1h' }));
    app.get(/^\/(?!api\/|auth\/).*/, (req, res) => res.sendFile(path.join(dist, 'index.html')));
  } else {
    app.get('/', (req, res) => res.type('text').send('Client not built. Run: npm --prefix web/client run build'));
  }

  // ---------------------------------------------------------------- errors

  // eslint-disable-next-line no-unused-vars
  app.use((err, req, res, next) => {
    if (err instanceof ActionRefused) return res.status(400).json({ error: err.message });
    if (err instanceof GitHubError) {
      if (err.status === 401) {
        store.deleteSession(sessionOf(req).id);
        return res.status(401).json({ error: err.message, signedOut: true });
      }
      return res.status(502).json({ error: err.message });
    }
    if (err?.type === 'entity.too.large') return res.status(413).json({ error: 'Request too large.' });
    if (err?.type === 'entity.parse.failed') return res.status(400).json({ error: 'Invalid JSON.' });
    console.error(err);
    res.status(500).json({ error: err?.message?.startsWith('The agent service') ? err.message : 'Internal error.' });
  });

  return app;
}
