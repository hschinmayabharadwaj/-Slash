import React, { useState, useEffect, useCallback } from 'react';
import { ScrollText, ExternalLink } from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';
import { fetchAudit } from '../services/api';

const ACTION_ICON = {
  task_created: '🆕',
  task_cancelled: '✖️',
  approved: '👍',
  rejected: '👎',
  cancelled: '✖️',
  expired: '⏰',
  kill_switch_on: '🔴',
  kill_switch_off: '🟢',
};

export default function Audit() {
  const [events, setEvents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetchAudit();
      setEvents(res.events || []);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 10_000);
    return () => clearInterval(id);
  }, [load]);

  return (
    <div className="audit-page">
      <div className="section-card card-hover">
        <div className="card-header">
          <ScrollText size={16} />
          <h3>Audit Trail</h3>
          <span className="card-header-sub">Latest events from the audit table (auto-refreshes)</span>
        </div>

        {error && <div className="error-banner">Error: {error}</div>}

        <div className="table-wrapper">
          <table className="task-table">
            <thead>
              <tr>
                <th>Action</th>
                <th>Task</th>
                <th>Status</th>
                <th>Detail</th>
                <th>When</th>
              </tr>
            </thead>
            <tbody>
              {loading && events.length === 0 && (
                <tr><td colSpan={5} className="table-empty"><div className="loading-spinner" /></td></tr>
              )}
              {!loading && events.length === 0 && (
                <tr><td colSpan={5} className="table-empty">No audit events yet</td></tr>
              )}
              {events.map((ev, idx) => {
                const detail = typeof ev.detail === 'string' ? ev.detail : JSON.stringify(ev.detail || {});
                const isTaskLink = Boolean(ev.taskId) && ev.action !== 'kill_switch_on' && ev.action !== 'kill_switch_off';
                return (
                  <tr key={ev.pk || idx}>
                    <td className="audit-action">
                      {ACTION_ICON[ev.action] || '•'} {ev.action}
                    </td>
                    <td className="audit-task">
                      {isTaskLink ? (
                        <span className="audit-task-id">{ev.taskId}</span>
                      ) : (ev.taskId || '—')}
                    </td>
                    <td>
                      {ev.status ? (
                        <span className={`status-badge status-badge--${String(ev.status).toLowerCase()}`}>
                          {ev.status}
                        </span>
                      ) : '—'}
                    </td>
                    <td className="audit-detail" title={detail}>{detail.slice(0, 90)}{detail.length > 90 ? '…' : ''}</td>
                    <td className="task-date">
                      {ev.ts ? formatDistanceToNow(new Date(ev.ts), { addSuffix: true }) : '—'}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}