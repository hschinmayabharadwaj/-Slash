import React, { useState, useEffect, useCallback } from 'react';
import { formatDistanceToNow, format } from 'date-fns';
import { Search, Filter, Download, ChevronLeft, ChevronRight, ExternalLink, XCircle } from 'lucide-react';
import { fetchTasks, cancelTask } from '../services/api';

// ─── Constants ────────────────────────────────────────────────────────────────

const ALL_STATUSES = [
  'all', 'pending', 'planning', 'awaiting_approval',
  'implementing', 'awaiting_impl_approval', 'creating_pr',
  'completed', 'failed',
];

const STATUS_LABELS = {
  all: 'All Statuses',
  pending: 'Pending',
  planning: 'Planning',
  awaiting_approval: 'Awaiting Approval',
  implementing: 'Implementing',
  awaiting_impl_approval: 'Awaiting Diff Review',
  creating_pr: 'Creating PR',
  completed: 'Completed',
  failed: 'Failed',
};

const PAGE_SIZE = 10;

// ─── Status badge ─────────────────────────────────────────────────────────────

function StatusBadge({ status }) {
  return (
    <span className={`status-badge status-badge--${status}`}>
      {STATUS_LABELS[status] || status}
    </span>
  );
}

// ─── Duration formatter ───────────────────────────────────────────────────────

function formatDuration(ms) {
  if (!ms) return '—';
  const minutes = Math.floor(ms / 60000);
  const seconds = Math.floor((ms % 60000) / 1000);
  if (minutes === 0) return `${seconds}s`;
  return `${minutes}m ${seconds}s`;
}

// ─── CSV export ───────────────────────────────────────────────────────────────

function exportCSV(tasks) {
  const headers = ['ID', 'Repository', 'Description', 'Status', 'User', 'Created', 'Duration', 'PR'];
  const rows = tasks.map((t) => [
    t.shortId,
    t.repository,
    `"${t.description.replace(/"/g, '""')}"`,
    t.status,
    t.username || t.user,
    t.createdAt ? format(new Date(t.createdAt), 'yyyy-MM-dd HH:mm') : '',
    formatDuration(t.durationMs),
    t.prUrl || '',
  ]);

  const csv = [headers.join(','), ...rows.map((r) => r.join(','))].join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `tasks-${format(new Date(), 'yyyy-MM-dd')}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

// ─── Main component ───────────────────────────────────────────────────────────

export default function TaskList({ onTaskClick, initialStatusFilter }) {
  const [tasks, setTasks] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState(initialStatusFilter || 'all');
  const [repoFilter, setRepoFilter] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [showFilters, setShowFilters] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetchTasks({
        search,
        status: statusFilter,
        repo: repoFilter,
        dateFrom,
        dateTo,
        page,
        pageSize: PAGE_SIZE,
      });
      setTasks(res.tasks);
      setTotal(res.total);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [search, statusFilter, repoFilter, dateFrom, dateTo, page]);

  useEffect(() => {
    load();
  }, [load]);

  // Reset to page 1 when filters change
  useEffect(() => {
    setPage(1);
  }, [search, statusFilter, repoFilter, dateFrom, dateTo]);

  const totalPages = Math.ceil(total / PAGE_SIZE);

  async function handleExport() {
    try {
      const res = await fetchTasks({
        search, status: statusFilter, repo: repoFilter, dateFrom, dateTo,
        page: 1, pageSize: 1000,
      });
      exportCSV(res.tasks);
    } catch (e) {
      alert('Export failed: ' + e.message);
    }
  }

  const CANCELLABLE = ['pending', 'planning', 'awaiting_approval', 'implementing', 'awaiting_impl_approval'];

  async function handleCancel(task) {
    if (!window.confirm(`Cancel task #${task.shortId}?`)) return;
    try {
      await cancelTask(task.id);
      load();
    } catch (e) {
      alert('Cancel failed: ' + e.message);
    }
  }

  return (
    <div className="task-list-page">
      {/* Toolbar */}
      <div className="toolbar">
        <div className="search-box">
          <Search size={15} />
          <input
            type="text"
            placeholder="Search tasks, repos..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>

        <div className="toolbar-right">
          <select
            className="select-input"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
          >
            {ALL_STATUSES.map((s) => (
              <option key={s} value={s}>{STATUS_LABELS[s]}</option>
            ))}
          </select>

          <button
            className={`btn-secondary ${showFilters ? 'btn-secondary--active' : ''}`}
            onClick={() => setShowFilters((v) => !v)}
          >
            <Filter size={14} />
            <span>Filters</span>
          </button>

          <button className="btn-secondary" onClick={handleExport}>
            <Download size={14} />
            <span>Export CSV</span>
          </button>
        </div>
      </div>

      {/* Advanced filters */}
      {showFilters && (
        <div className="filter-panel">
          <div className="filter-group">
            <label>Repository</label>
            <input
              type="text"
              placeholder="e.g. api-service"
              value={repoFilter}
              onChange={(e) => setRepoFilter(e.target.value)}
            />
          </div>
          <div className="filter-group">
            <label>From</label>
            <input
              type="date"
              value={dateFrom}
              onChange={(e) => setDateFrom(e.target.value)}
            />
          </div>
          <div className="filter-group">
            <label>To</label>
            <input
              type="date"
              value={dateTo}
              onChange={(e) => setDateTo(e.target.value)}
            />
          </div>
          <button
            className="btn-secondary"
            onClick={() => {
              setSearch('');
              setStatusFilter('all');
              setRepoFilter('');
              setDateFrom('');
              setDateTo('');
            }}
          >
            Clear all
          </button>
        </div>
      )}

      {/* Summary line */}
      <div className="results-line">
        {loading ? 'Loading…' : `${total} task${total !== 1 ? 's' : ''} found`}
      </div>

      {/* Error */}
      {error && <div className="error-banner">Error: {error}</div>}

      {/* Table */}
      <div className="table-wrapper">
        <table className="task-table">
          <thead>
            <tr>
              <th>#</th>
              <th>Repository</th>
              <th>Description</th>
              <th>Status</th>
              <th>User</th>
              <th>Created</th>
              <th>PR</th>
              <th>Duration</th>
              <th>Actions</th>
              </tr>
            </thead>
          <tbody>
            {loading && tasks.length === 0 && (
              <tr>
                <td colSpan={9} className="table-empty">
                  <div className="loading-spinner" />
                </td>
              </tr>
            )}
            {!loading && tasks.length === 0 && (
              <tr>
                <td colSpan={9} className="table-empty">No tasks found</td>
              </tr>
            )}
            {tasks.map((task) => (
              <tr
                key={task.id}
                className="task-row"
                onClick={() => onTaskClick && onTaskClick(task.id)}
              >
                <td className="task-id">#{task.shortId}</td>
                <td className="task-repo">
                  <span className="repo-badge">{task.repository}</span>
                </td>
                <td className="task-desc">
                  <span title={task.description}>
                    {task.description.length > 70
                      ? task.description.slice(0, 70) + '…'
                      : task.description}
                  </span>
                </td>
                <td>
                  <StatusBadge status={task.status} />
                </td>
                <td className="task-user">{task.username || task.user}</td>
                <td className="task-date">
                  {task.createdAt
                    ? formatDistanceToNow(new Date(task.createdAt), { addSuffix: true })
                    : '—'}
                </td>
                <td className="task-pr" onClick={(e) => e.stopPropagation()}>
                  {task.prUrl ? (
                    <a
                      href={task.prUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="pr-link"
                    >
                      #{task.prNumber}
                      <ExternalLink size={11} />
                    </a>
                  ) : '—'}
                </td>
                <td className="task-duration">{formatDuration(task.durationMs)}</td>
                <td className="task-actions" onClick={(e) => e.stopPropagation()}>
                  {CANCELLABLE.includes(task.status) ? (
                    <button
                      className="btn-icon btn-danger"
                      title="Cancel task"
                      onClick={() => handleCancel(task)}
                    >
                      <XCircle size={15} />
                    </button>
                  ) : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="pagination">
          <button
            className="btn-secondary"
            disabled={page <= 1}
            onClick={() => setPage((p) => p - 1)}
          >
            <ChevronLeft size={16} />
          </button>
          <span className="pagination-info">
            Page {page} of {totalPages}
          </span>
          <button
            className="btn-secondary"
            disabled={page >= totalPages}
            onClick={() => setPage((p) => p + 1)}
          >
            <ChevronRight size={16} />
          </button>
        </div>
      )}
    </div>
  );
}
