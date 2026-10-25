import { useCallback, useEffect, useState } from 'react';
import { api } from '../api.js';
import Diff, { safeGitHubUrl } from '../components/Diff.jsx';
import Markdown from '../components/Markdown.jsx';
import { Link, navigate } from '../router.jsx';

const STEPS = [
  ['plan', 'Plan', ['pending', 'planning']],
  ['approve-plan', 'Scope approval', ['awaiting_plan_approval']],
  ['build', 'Change + checks', ['plan_approved', 'implementing', 'verifying']],
  ['approve-diff', 'Review diff', ['awaiting_impl_approval']],
  ['pr', 'Pull request', ['impl_approved', 'creating_pr']],
  ['review', 'Engineer review on GitHub', ['completed']],
];
const ENDED = {
  needs_info: 'The agent needs more information', declined: 'The agent declined this request',
  plan_rejected: 'Plan rejected', impl_rejected: 'Changes rejected', failed: 'Failed', cancelled: 'Cancelled',
  lost: 'Lost (the agent restarted)',
};
const WORKING = new Set(['pending', 'planning', 'plan_approved', 'implementing', 'verifying', 'impl_approved', 'creating_pr']);

export default function Task({ id, onError }) {
  const [t, setT] = useState(null);
  const [busy, setBusy] = useState(false);
  const [answers, setAnswers] = useState('');

  const load = useCallback(() => api.task(id).then(setT).catch(onError), [id, onError]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!t || !WORKING.has(t.status)) return undefined;
    const timer = setTimeout(load, 3000);
    return () => clearTimeout(timer);
  }, [t, load]);

  const act = (fn) => async () => {
    setBusy(true);
    try { setT(await fn()); } catch (e) { onError(e); load(); } finally { setBusy(false); }
  };
  const clarify = async () => {
    setBusy(true);
    try {
      const { id: next } = await api.clarify(id, answers);
      navigate(`/tasks/${encodeURIComponent(next)}`);
    } catch (e) { onError(e); } finally { setBusy(false); }
  };

  if (!t) return <p className="muted">Loading…</p>;
  const plan = t.plan || {};
  const stepIndex = STEPS.findIndex(([, , statuses]) => statuses.includes(t.status));
  const prUrl = safeGitHubUrl(t.prUrl);

  return (
    <>
      <section className="card">
        <p className="muted small"><Link to={`/repos/${encodeURIComponent(t.owner)}/${encodeURIComponent(t.name)}`}>{t.fullName}</Link> · requested by @{t.requester}</p>
        <h1>{(t.request || '').split('\n')[0].slice(0, 120)}</h1>
        {stepIndex >= 0 ? (
          <ol className="steps">
            {STEPS.map(([key, label], i) => (
              <li key={key} className={i < stepIndex ? 'done' : i === stepIndex ? 'now' : ''}>{label}</li>
            ))}
          </ol>
        ) : <p className="error">{ENDED[t.status] || t.status}</p>}
        {t.message && <p className="muted">{t.message}</p>}
        {WORKING.has(t.status) && <p className="muted small">Working… this page updates by itself.</p>}
      </section>

      {plan.summary && (
        <section className="card">
          <h2>Plan</h2>
          <Markdown text={plan.summary} />
          <dl className="facts">
            <dt>Risk</dt><dd><span className={`risk ${plan.risk}`}>{plan.risk}</span> {plan.risk_reason}</dd>
            {plan.files?.length > 0 && <><dt>Files</dt><dd>{plan.files.map((f) => <code key={f}>{f} </code>)}</dd></>}
            {plan.how_to_verify && <><dt>How to verify</dt><dd>{plan.how_to_verify}</dd></>}
          </dl>
          {plan.acceptance_criteria?.length > 0 && (
            <>
              <h3>Acceptance criteria</h3>
              <ul>{plan.acceptance_criteria.map((c) => <li key={c}>{c}</li>)}</ul>
            </>
          )}
          {plan.questions?.length > 0 && (
            <>
              <h3>Questions from the agent</h3>
              <ul>{plan.questions.map((q) => <li key={q}>{q}</li>)}</ul>
            </>
          )}
          {t.status === 'awaiting_plan_approval' && (
            <div className="row">
              <button type="button" className="btn primary" disabled={busy || !t.can.decidePlan}
                onClick={act(() => api.decidePlan(id, true))}>Approve plan</button>
              <button type="button" className="btn" disabled={busy || !t.can.rejectPlan}
                onClick={act(() => api.decidePlan(id, false))}>Reject</button>
              {t.can.why && <span className="muted small">{t.can.why}</span>}
            </div>
          )}
          {t.can.clarify && (
            <div>
              <label htmlFor="answers">Your answers</label>
              <textarea id="answers" className="short" value={answers} maxLength={1500}
                onChange={(e) => setAnswers(e.target.value)} />
              <div className="row">
                <button type="button" className="btn primary" disabled={busy || !answers.trim()} onClick={clarify}>
                  Send answers and try again
                </button>
              </div>
            </div>
          )}
        </section>
      )}

      {t.diff && (
        <section className="card">
          <h2>Proposed change</h2>
          {t.summary && <Markdown text={t.summary} />}
          <p className="muted small">
            Files: {(t.files || []).map((f) => <code key={f}>{f} </code>)} · diff sha256 <code>{(t.diffSha256 || '').slice(0, 12)}</code>
          </p>
          <Diff text={t.diff} />
          {t.diffTruncated && <p className="muted small">Diff truncated for display; the pull request has all of it.</p>}
          <h3>Checks {t.checksOk ? <span className="ok">passed</span> : <span className="error">failed</span>}</h3>
          <pre className="code">{t.checks}</pre>
          {t.status === 'awaiting_impl_approval' && (
            <div className="row">
              <button type="button" className="btn primary" disabled={busy || !t.can.decideChanges}
                onClick={act(() => api.decideChanges(id, true))}>
                {busy ? 'Opening pull request…' : 'Approve diff & open pull request'}
              </button>
              <button type="button" className="btn" disabled={busy || !t.can.decideChanges}
                onClick={act(() => api.decideChanges(id, false))}>Reject</button>
              {t.can.why && <span className="muted small">{t.can.why}</span>}
            </div>
          )}
        </section>
      )}

      {t.status === 'completed' && (
        <section className="card">
          <p className="ok">
            ✓ Pull request opened{prUrl && <>: <a href={prUrl} target="_blank" rel="noreferrer noopener">{prUrl}</a></>}
          </p>
          <p className="muted">
            Nothing has been merged. An engineer reviews it on GitHub; the repository's branch protection decides who
            can approve and merge. The agent can't approve or merge its own pull requests.
          </p>
        </section>
      )}
      <p><Link to="/tasks">← All tasks</Link></p>
    </>
  );
}
