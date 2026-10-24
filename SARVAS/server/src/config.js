// Loads the shared config.json (also read by the Go agent). String values may
// reference environment variables as ${NAME}.
import fs from 'node:fs';
import path from 'node:path';

const DEFAULTS = {
  web: {
    host: '127.0.0.1',
    port: 8765,
    baseUrl: 'http://localhost:8765',
    sessionHours: 8,
    dataDir: './data',
    clientDist: './client/dist',
  },
  github: { clientId: '', clientSecret: '' },
  agentService: { url: 'http://127.0.0.1:8766', token: '' },
  security: { allowedRepos: [], maxChangesPerHour: 10, killSwitchFile: './data/KILL_SWITCH' },
  repos: {},
};

function expand(value) {
  if (typeof value === 'string') {
    return value.replace(/\$\{([A-Za-z_][A-Za-z0-9_]*)\}/g, (_, name) => process.env[name] ?? '');
  }
  if (Array.isArray(value)) return value.map(expand);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, expand(v)]));
  }
  return value;
}

function merge(base, extra) {
  const out = { ...base };
  for (const [k, v] of Object.entries(extra ?? {})) {
    out[k] = v && typeof v === 'object' && !Array.isArray(v) && base[k] ? merge(base[k], v) : v;
  }
  return out;
}

export function loadConfig(configPath = 'config.json') {
  const raw = fs.existsSync(configPath) ? JSON.parse(fs.readFileSync(configPath, 'utf8')) : {};
  const config = merge(DEFAULTS, expand(raw));
  config.github.clientId ||= process.env.GITHUB_CLIENT_ID ?? '';
  config.github.clientSecret ||= process.env.GITHUB_CLIENT_SECRET ?? '';
  config.agentService.token ||= process.env.AGENT_SERVICE_TOKEN ?? '';
  // Relative paths are relative to the config file, not the working directory.
  const dir = path.dirname(path.resolve(configPath));
  for (const [section, key] of [['web', 'dataDir'], ['web', 'clientDist'], ['security', 'killSwitchFile']]) {
    config[section][key] = path.resolve(dir, config[section][key]);
  }
  validate(config);
  return config;
}

export function validate(config) {
  if (!config.github.clientId || !config.github.clientSecret) {
    throw new Error('github.clientId and github.clientSecret (your GitHub App OAuth credentials) are required');
  }
  const base = new URL(config.web.baseUrl);
  const local = ['localhost', '127.0.0.1'].includes(base.hostname);
  if (base.protocol !== 'https:' && !(base.protocol === 'http:' && local)) {
    throw new Error('web.baseUrl must be https://, or http://localhost / http://127.0.0.1');
  }
  if ((config.agentService.token ?? '').length < 32) {
    throw new Error('agentService.token (AGENT_SERVICE_TOKEN) must be a random value of at least 32 characters');
  }
}

export function repoAllowed(config, fullName) {
  const repo = config.repos?.[fullName];
  if (repo && repo.enabled === false) return false;
  const allowed = config.security.allowedRepos ?? [];
  return allowed.length === 0 || allowed.some((r) => r.toLowerCase() === fullName.toLowerCase());
}
