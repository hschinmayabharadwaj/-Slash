// One vocabulary for task status across the list, the task page and the dashboard.
const STATUS = {
  pending: ['Queued', 'working'],
  planning: ['Planning', 'working'],
  awaiting_plan_approval: ['Plan ready for review', 'attention'],
  plan_approved: ['Plan approved', 'working'],
  implementing: ['Making the change', 'working'],
  verifying: ['Running checks', 'working'],
  awaiting_impl_approval: ['Diff ready for review', 'attention'],
  impl_approved: ['Approved', 'working'],
  creating_pr: ['Opening pull request', 'working'],
  completed: ['Pull request opened', 'done'],
  needs_info: ['Needs more information', 'attention'],
  declined: ['Declined', 'stopped'],
  plan_rejected: ['Plan rejected', 'stopped'],
  impl_rejected: ['Changes rejected', 'stopped'],
  failed: ['Failed', 'failed'],
  cancelled: ['Cancelled', 'stopped'],
  lost: ['Lost (the agent restarted)', 'failed'],
};

export function taskStatus(status) {
  const [label, tone] = STATUS[status] || [String(status).replaceAll('_', ' '), 'stopped'];
  return { label, tone };
}

export const TONE_CLASS = {
  working: 'border-primary/30 bg-primary/10 text-primary',
  attention: 'border-warn/30 bg-warn/10 text-warn',
  done: 'border-ok/30 bg-ok/10 text-ok',
  failed: 'border-bad/30 bg-bad/10 text-bad',
  stopped: 'text-muted-foreground',
};

export const firstLine = (text, max = 120) => (text || '').split('\n')[0].slice(0, max) || '(no text)';
export const utc = (iso) => iso.slice(0, 19).replace('T', ' ');
