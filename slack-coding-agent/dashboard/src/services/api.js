import { fetchAuthSession } from 'aws-amplify/auth';
import {
  MOCK_TASKS,
  computeStats,
  computeHourlyData,
  computeStatusDist,
  MOCK_ACTIVITY,
  MOCK_INFRA_STATUS,
} from './mockData';

const SAME_ORIGIN = process.env.REACT_APP_SAME_ORIGIN === 'true';
const API_URL = process.env.REACT_APP_API_URL;
const API_BASE = (API_URL || '').replace(/\/+$/, '');
// Same-origin mode (site Lambda behind the HTTP API) serves the SPA and the
// dashboard API from one https origin, so API_BASE stays empty and no mock is used.
const USE_MOCK = !SAME_ORIGIN && !API_URL;

// ─── Auth helpers ─────────────────────────────────────────────────────────────

async function getAuthHeaders() {
  try {
    const session = await fetchAuthSession();
    const token = session?.tokens?.idToken?.toString();
    return token ? { Authorization: `Bearer ${token}` } : {};
  } catch {
    return {};
  }
}

async function apiFetch(path, options = {}) {
  const headers = await getAuthHeaders();
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...headers,
      ...(options.headers || {}),
    },
  });

  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`API error ${res.status}: ${body}`);
  }

  return res.json();
}

// ─── Mock helpers ─────────────────────────────────────────────────────────────

function sleep(ms = 300) {
  return new Promise((r) => setTimeout(r, ms));
}

function applyFilters(tasks, filters = {}) {
  let result = [...tasks];

  if (filters.status && filters.status !== 'all') {
    result = result.filter((t) => t.status === filters.status);
  }
  if (filters.repo) {
    result = result.filter((t) =>
      t.repository.toLowerCase().includes(filters.repo.toLowerCase())
    );
  }
  if (filters.search) {
    const q = filters.search.toLowerCase();
    result = result.filter(
      (t) =>
        t.description.toLowerCase().includes(q) ||
        t.repository.toLowerCase().includes(q) ||
        String(t.shortId).includes(q)
    );
  }
  if (filters.dateFrom) {
    result = result.filter((t) => new Date(t.createdAt) >= new Date(filters.dateFrom));
  }
  if (filters.dateTo) {
    result = result.filter((t) => new Date(t.createdAt) <= new Date(filters.dateTo));
  }

  // Sort newest first
  result.sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt));

  return result;
}

// ─── Public API ───────────────────────────────────────────────────────────────

export async function fetchTasks(filters = {}) {
  if (USE_MOCK) {
    await sleep(200);
    const tasks = applyFilters(MOCK_TASKS, filters);
    const page = filters.page || 1;
    const pageSize = filters.pageSize || 20;
    const start = (page - 1) * pageSize;
    return {
      tasks: tasks.slice(start, start + pageSize),
      total: tasks.length,
      page,
      pageSize,
    };
  }

  const params = new URLSearchParams();
  Object.entries(filters).forEach(([k, v]) => v !== undefined && v !== '' && params.set(k, v));
  const data = await apiFetch(`/tasks`);
  const display = (data.tasks || []).map(backendToDisplay);
  const filtered = applyFilters(display, filters);
  const page = filters.page || 1;
  const pageSize = filters.pageSize || 20;
  const start = (page - 1) * pageSize;
  return {
    tasks: filtered.slice(start, start + pageSize),
    total: filtered.length,
    page,
    pageSize,
  };
}

function backendToDisplay(t) {
  const shortId = String(t.taskId || '').replace('task-', '').slice(0, 6);
  return {
    id: t.taskId,
    shortId,
    repository: t.repo || '',
    description: t.request || '',
    status: STATUS_LABEL[t.status] || (t.status || '').toLowerCase(),
    username: t.source || 'dashboard',
    user: t.source || 'dashboard',
    createdAt: t.createdAt,
    durationMs: null,
    prUrl: t.prUrl,
    prNumber: t.prNumber,
    sim: t.sim === true || t.sim === 'true' || t.sim === 1,
    model: t.model || t.modelBackend || null,
  };
}

export async function fetchTask(id) {
  if (USE_MOCK) {
    await sleep(150);
    const task = MOCK_TASKS.find((t) => t.id === id || String(t.shortId) === String(id));
    if (!task) throw new Error(`Task ${id} not found`);
    return task;
  }
  const data = await apiFetch(`/tasks/${id}`);
  const raw = data.item || data;
  return raw && raw.taskId ? backendToDisplay(raw) : raw;
}

export async function createTask({ request, repo, baseBranch, client_token }) {
  if (USE_MOCK) {
    await sleep(400);
    return { taskId: `task-${Math.random().toString(16).slice(2, 12)}`, status: 'READY', duplicate: false };
  }
  return apiFetch('/tasks', {
    method: 'POST',
    body: JSON.stringify({ request, repo, baseBranch, client_token }),
  });
}

export async function cancelTask(id) {
  if (USE_MOCK) {
    await sleep(400);
    return { success: true, message: `Task ${id} cancelled (mock)` };
  }
  return apiFetch(`/tasks/${id}/cancel`, { method: 'POST', body: '{}' });
}

export async function fetchMetrics() {
  if (USE_MOCK) {
    await sleep(100);
    const stats = computeStats(MOCK_TASKS);
    return { updatedAt: new Date().toISOString(), counts: stats?.pipelineCounts || {}, total: MOCK_TASKS.length, inFlight: 0 };
  }
  return apiFetch('/metrics');
}

export async function fetchAudit() {
  if (USE_MOCK) {
    await sleep(100);
    return { events: [], count: 0 };
  }
  const data = await apiFetch('/audit');
  return { events: data.events || [], count: data.count ?? (data.events || []).length };
}

export async function fetchKillSwitch() {
  if (USE_MOCK) return { killSwitch: false, value: '0' };
  return apiFetch('/admin/kill');
}

export async function setKillSwitch(kill) {
  if (USE_MOCK) return { killSwitch: kill, value: kill ? '1' : '0' };
  return apiFetch('/admin/kill', { method: 'POST', body: JSON.stringify({ kill }) });
}

// Map our UPPERCASE statuses to the display labels the dashboard uses.
const STATUS_LABEL = {
  READY: 'pending',
  RUNNING: 'implementing',
  AWAITING_APPROVAL: 'awaiting_approval',
  PLAN_APPROVED: 'planning',
  AWAITING_IMPL_APPROVAL: 'awaiting_impl_approval',
  IMPL_APPROVED: 'implementing',
  COMPLETED: 'completed',
  REJECTED: 'failed',
  CANCELLED: 'failed',
  FAILED: 'failed',
  EXPIRED: 'failed',
  DECLINED: 'failed',
};

export async function fetchStats() {
  if (USE_MOCK) {
    await sleep(100);
    const stats = computeStats(MOCK_TASKS);
    const hourly = computeHourlyData(MOCK_TASKS);
    const statusDist = computeStatusDist(MOCK_TASKS);
    return {
      ...stats,
      hourlyData: hourly,
      statusDistribution: statusDist,
      recentActivity: MOCK_ACTIVITY,
      infraStatus: MOCK_INFRA_STATUS,
      pipelineCounts: MOCK_TASKS.reduce((acc, t) => {
        acc[t.status] = (acc[t.status] || 0) + 1;
        return acc;
      }, {}),
    };
  }

  const [stats, metrics] = await Promise.all([apiFetch('/stats'), apiFetch('/metrics')]);
  const counts = stats.counts || {};
  const total = stats.total ?? metrics.total ?? 0;
  const displayed = {};
  Object.entries(counts).forEach(([status, value]) => {
    const label = STATUS_LABEL[status] || status.toLowerCase();
    displayed[label] = (displayed[label] || 0) + value;
  });
  return {
    total,
    totalYesterday: 0,
    completedToday: counts.COMPLETED || 0,
    failed: (counts.FAILED || 0) + (counts.REJECTED || 0) + (counts.EXPIRED || 0),
    activeNow: metrics.inFlight ?? 0,
    pipelineCounts: displayed,
    hourlyData: [],
    statusDistribution: Object.entries(displayed).map(([name, value]) => ({ name, value })),
    recentActivity: [],
    infraStatus: null,
    modelBackend: stats.modelBackend || metrics.modelBackend || null,
    simMode: stats.simMode === true || metrics.simMode === true || false,
  };
}

export async function approveTask(id, type) {
  if (USE_MOCK) {
    await sleep(500);
    return { success: true, message: `Task ${id} ${type} approved (mock)` };
  }
  return apiFetch(`/tasks/${id}/approve`, {
    method: 'POST',
    body: JSON.stringify({ note: type }),
  });
}

export async function rejectTask(id, type) {
  if (USE_MOCK) {
    await sleep(500);
    return { success: true, message: `Task ${id} ${type} rejected (mock)` };
  }
  return apiFetch(`/tasks/${id}/reject`, {
    method: 'POST',
    body: JSON.stringify({ note: type }),
  });
}
