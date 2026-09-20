import React, { useState, useEffect } from 'react';
import { format, formatDistanceToNow, differenceInSeconds } from 'date-fns';
import {
  ArrowLeft, ExternalLink, ChevronDown, ChevronUp,
  CheckCircle2, XCircle, Clock, Code, GitPullRequest,
  ThumbsUp, ThumbsDown, AlertTriangle,
} from 'lucide-react';
import { fetchTask, approveTask, rejectTask } from '../services/api';

// ─── Status badge ─────────────────────────────────────────────────────────────

const STATUS_LABELS = {
  pending: 'Pending',
  planning: 'Planning',
  awaiting_approval: 'Awaiting Approval',
  implementing: 'Implementing',
  awaiting_impl_approval: 'Awaiting Diff Review',
  creating_pr: 'Creating PR',
  completed: 'Completed',
  failed: 'Failed',
};

function StatusBadge({ status }) {
  return (
    <span className={`status-badge status-badge--${status} status-badge--lg`}>
      {STATUS_LABELS[status] || status}
    </span>
  );
}

// ─── Timeline ─────────────────────────────────────────────────────────────────

const TIMELINE_ICONS = {
  pending: Clock,
  planning: Code,
  awaiting_approval: AlertTriangle,
  implementing: Code,
  awaiting_impl_approval: AlertTriangle,
  creating_pr: GitPullRequest,
  completed: CheckCircle2,
  failed: XCircle,
};

function Timeline({ entries }) {
  if (!entries || entries.length === 0) return null;

  return (
    <div className="timeline">
      {entries.map((entry, i) => {
        const Icon = TIMELINE_ICONS[entry.status] || Clock;
        const isLast = i === entries.length - 1;
        const nextEntry = entries[i + 1];
        const durationSec = nextEntry
          ? differenceInSeconds(new Date(nextEntry.timestamp), new Date(entry.timestamp))
          : null;

        return (
          <div key={i} className={`timeline-entry ${isLast ? 'timeline-entry--last' : ''}`}>
            <div className="timeline-left">
              <div className={`timeline-dot timeline-dot--${entry.status}`}>
                <Icon size={12} />
              </div>
              {!isLast && <div className="timeline-line" />}
            </div>
            <div className="timeline-body">
              <div className="timeline-status">{STATUS_LABELS[entry.status] || entry.status}</div>
              <div className="timeline-time">
                {format(new Date(entry.timestamp), 'MMM d, HH:mm:ss')}
              </div>
              {entry.note && <div className="timeline-note">{entry.note}</div>}
              {durationSec !== null && (
                <div className="timeline-duration">
                  Spent {durationSec >= 60
                    ? `${Math.floor(durationSec / 60)}m ${durationSec % 60}s`
                    : `${durationSec}s`} in this stage
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ─── Plan viewer ──────────────────────────────────────────────────────────────

function PlanSection({ plan }) {
  if (!plan) return null;

  // Very simple markdown-like rendering
  const lines = plan.split('\n');
  return (
    <div className="plan-content">
      {lines.map((line, i) => {
        if (line.startsWith('## ')) {
          return <h3 key={i} className="plan-h2">{line.slice(3)}</h3>;
        }
        if (line.startsWith('### ')) {
          return <h4 key={i} className="plan-h3">{line.slice(4)}</h4>;
        }
        if (line.startsWith('- ') || line.startsWith('* ')) {
          return <li key={i} className="plan-li">{line.slice(2)}</li>;
        }
        if (line.match(/^\d+\. /)) {
          return <li key={i} className="plan-li plan-li--numbered">{line.replace(/^\d+\. /, '')}</li>;
        }
        if (line.startsWith('`') && line.endsWith('`') && line.length > 2) {
          return <code key={i} className="plan-code-inline">{line.slice(1, -1)}</code>;
        }
        if (line.trim() === '') return <div key={i} className="plan-spacer" />;
        return <p key={i} className="plan-para">{line}</p>;
      })}
    </div>
  );
}

// ─── Diff viewer ─────────────────────────────────────────────────────────────

function DiffViewer({ diff }) {
  if (!diff) return <p className="empty-state">No diff available yet.</p>;

  const lines = diff.split('\n');
  return (
    <div className="diff-viewer">
      {lines.map((line, i) => {
        let cls = 'diff-line';
        if (line.startsWith('+') && !line.startsWith('+++')) cls += ' diff-line--added';
        else if (line.startsWith('-') && !line.startsWith('---')) cls += ' diff-line--removed';
        else if (line.startsWith('@@')) cls += ' diff-line--hunk';
        else if (line.startsWith('diff ') || line.startsWith('index ') || line.startsWith('---') || line.startsWith('+++')) cls += ' diff-line--meta';

        return (
          <div key={i} className={cls}>
            <span className="diff-line-num">{i + 1}</span>
            <pre className="diff-line-content">{line}</pre>
          </div>
        );
      })}
    </div>
  );
}

// ─── Checks section ───────────────────────────────────────────────────────────

function ChecksSection({ checks }) {
  if (!checks) return <p className="empty-state">No checks run yet.</p>;

  return (
    <div className="checks-section">
      {Object.entries(checks).map(([key, check]) => (
        <div key={key} className={`check-card check-card--${check.status}`}>
          <div className="check-header">
            {check.status === 'passed'
              ? <CheckCircle2 size={16} color="#3fb950" />
              : <XCircle size={16} color="#f85149" />}
            <span className="check-name">{key === 'lint' ? 'Lint' : 'Tests'}</span>
            <span className={`check-status check-status--${check.status}`}>
              {check.status}
            </span>
            {check.duration && (
              <span className="check-duration">{(check.duration / 1000).toFixed(1)}s</span>
            )}
          </div>
          {check.output && (
            <pre className="check-output">{check.output}</pre>
          )}
        </div>
      ))}
    </div>
  );
}

// ─── Main component ───────────────────────────────────────────────────────────

export default function TaskDetail({ taskId, onBack }) {
  const [task, setTask] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [actionLoading, setActionLoading] = useState(false);
  const [actionMessage, setActionMessage] = useState(null);
  const [debugOpen, setDebugOpen] = useState(false);
  const [activeTab, setActiveTab] = useState('timeline');

  useEffect(() => {
    setLoading(true);
    setError(null);
    fetchTask(taskId)
      .then(setTask)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [taskId]);

  async function handleApprove(type) {
    setActionLoading(true);
    setActionMessage(null);
    try {
      await approveTask(taskId, type);
      setActionMessage({ type: 'success', text: `${type === 'plan' ? 'Plan' : 'Implementation'} approved!` });
      // Reload task
      const updated = await fetchTask(taskId);
      setTask(updated);
    } catch (e) {
      setActionMessage({ type: 'error', text: e.message });
    } finally {
      setActionLoading(false);
    }
  }

  async function handleReject(type) {
    setActionLoading(true);
    setActionMessage(null);
    try {
      await rejectTask(taskId, type);
      setActionMessage({ type: 'warning', text: `${type === 'plan' ? 'Plan' : 'Implementation'} rejected.` });
      const updated = await fetchTask(taskId);
      setTask(updated);
    } catch (e) {
      setActionMessage({ type: 'error', text: e.message });
    } finally {
      setActionLoading(false);
    }
  }

  if (loading) {
    return (
      <div className="task-detail-page">
        <button className="btn-back" onClick={onBack}>
          <ArrowLeft size={16} /> Back to Tasks
        </button>
        <div className="loading-center">
          <div className="loading-spinner loading-spinner--lg" />
          <p>Loading task…</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="task-detail-page">
        <button className="btn-back" onClick={onBack}>
          <ArrowLeft size={16} /> Back to Tasks
        </button>
        <div className="error-banner">Error: {error}</div>
      </div>
    );
  }

  if (!task) return null;

  const tabs = [
    { key: 'timeline', label: 'Timeline' },
    { key: 'plan', label: 'Plan' },
    { key: 'diff', label: 'Diff' },
    { key: 'checks', label: 'Checks' },
  ];

  return (
    <div className="task-detail-page">
      {/* Back button */}
      <button className="btn-back" onClick={onBack}>
        <ArrowLeft size={16} /> Back to Tasks
      </button>

      {/* Header */}
      <div className="detail-header card-hover">
        <div className="detail-header-top">
          <div className="detail-title">
            <span className="detail-task-id">Task #{task.shortId}</span>
            <StatusBadge status={task.status} />
            {task.sim ? (
              <span className="sim-badge" title={`Ran with SIM backend${task.model ? ` (${task.model})` : ''}`}>
                SIM
              </span>
            ) : task.model && task.model !== 'bedrock' ? (
              <span className="model-badge" title={`Model backend: ${task.model}`}>
                {task.model}
              </span>
            ) : null}
          </div>
          {task.prUrl && (
            <a
              href={task.prUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="pr-link-btn"
            >
              <GitPullRequest size={14} />
              View PR #{task.prNumber}
              <ExternalLink size={12} />
            </a>
          )}
        </div>

        <p className="detail-description">{task.description}</p>

        <div className="detail-meta">
          <span>
            <strong>Repo:</strong> {task.repository}
          </span>
          <span>
            <strong>User:</strong> {task.username || task.user}
          </span>
          <span>
            <strong>Created:</strong>{' '}
            {task.createdAt
              ? formatDistanceToNow(new Date(task.createdAt), { addSuffix: true })
              : '—'}
          </span>
          {task.updatedAt && (
            <span>
              <strong>Updated:</strong>{' '}
              {formatDistanceToNow(new Date(task.updatedAt), { addSuffix: true })}
            </span>
          )}
          {task.durationMs && (
            <span>
              <strong>Duration:</strong>{' '}
              {Math.floor(task.durationMs / 60000)}m {Math.floor((task.durationMs % 60000) / 1000)}s
            </span>
          )}
        </div>
      </div>

      {/* Action buttons for approval states */}
      {(task.status === 'awaiting_approval' || task.status === 'awaiting_impl_approval') && (
        <div className="action-panel">
          <div className="action-panel-title">
            <AlertTriangle size={16} color="#d29922" />
            {task.status === 'awaiting_approval'
              ? 'This task is awaiting plan approval'
              : 'This task is awaiting diff review'}
          </div>
          {actionMessage && (
            <div className={`action-message action-message--${actionMessage.type}`}>
              {actionMessage.text}
            </div>
          )}
          <div className="action-buttons">
            <button
              className="btn-approve"
              onClick={() => handleApprove(task.status === 'awaiting_approval' ? 'plan' : 'impl')}
              disabled={actionLoading}
            >
              <ThumbsUp size={15} />
              {actionLoading ? 'Processing…' : 'Approve'}
            </button>
            <button
              className="btn-reject"
              onClick={() => handleReject(task.status === 'awaiting_approval' ? 'plan' : 'impl')}
              disabled={actionLoading}
            >
              <ThumbsDown size={15} />
              Reject
            </button>
          </div>
        </div>
      )}

      {/* Tabs */}
      <div className="detail-tabs">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            className={`detail-tab ${activeTab === tab.key ? 'detail-tab--active' : ''}`}
            onClick={() => setActiveTab(tab.key)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div className="detail-tab-content section-card">
        {activeTab === 'timeline' && <Timeline entries={task.timeline} />}

        {activeTab === 'plan' && (
          task.plan
            ? <PlanSection plan={task.plan} />
            : <p className="empty-state">No plan available yet.</p>
        )}

        {activeTab === 'diff' && <DiffViewer diff={task.diff} />}

        {activeTab === 'checks' && <ChecksSection checks={task.checks} />}
      </div>

      {/* JSON debug panel */}
      <div className="debug-panel">
        <button
          className="debug-toggle"
          onClick={() => setDebugOpen((o) => !o)}
        >
          {debugOpen ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
          <span>Raw JSON</span>
        </button>
        {debugOpen && (
          <pre className="debug-json">
            {JSON.stringify(task, null, 2)}
          </pre>
        )}
      </div>
    </div>
  );
}
