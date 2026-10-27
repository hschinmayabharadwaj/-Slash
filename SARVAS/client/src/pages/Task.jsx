import { ArrowLeft, Check, CircleCheck, CircleX, ExternalLink, GitPullRequest } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api.js';
import Diff, { safeGitHubUrl } from '../components/Diff.jsx';
import Markdown from '../components/Markdown.jsx';
import { PageLoading } from '../components/Skeleton.jsx';
import StatusPill from '../components/StatusPill.jsx';
import ShimmerText from '../components/kokonutui/shimmer-text';
import { firstLine } from '../lib/status';
import { gsap } from '../lib/motion';
import { cn, reducedMotion } from '../lib/utils';
import { Link, navigate } from '../router.jsx';

const STEPS = [
  ['plan', 'Plan', ['pending', 'planning']],
  ['approve-plan', 'Scope approval', ['awaiting_plan_approval']],
  ['build', 'Change + checks', ['plan_approved', 'implementing', 'verifying']],
  ['approve-diff', 'Review diff', ['awaiting_impl_approval']],
  ['pr', 'Pull request', ['impl_approved', 'creating_pr']],
  ['review', 'Engineer review', ['completed']],
];
const ENDED = {
  needs_info: 'The agent needs more information', declined: 'The agent declined this request',
  plan_rejected: 'Plan rejected', impl_rejected: 'Changes rejected', failed: 'Failed', cancelled: 'Cancelled',
  lost: 'Lost (the agent restarted)',
};
const WORKING = new Set(['pending', 'planning', 'plan_approved', 'implementing', 'verifying', 'impl_approved', 'creating_pr']);
const RISK = { low: 'bg-ok/12 text-ok', medium: 'bg-warn/12 text-warn', high: 'bg-bad/10 text-bad' };

// Progress through the pipeline; the connecting bar eases forward (GSAP).
function Stepper({ index, done }) {
  const bar = useRef(null);
  const pct = done ? 100 : (index / (STEPS.length - 1)) * 100;
  useEffect(() => {
    const tween = gsap.to(bar.current, { width: `${pct}%`, duration: reducedMotion() ? 0 : 0.9, ease: 'expo.out' });
    return () => tween.kill();
  }, [pct]);

  return (
    <ol className="relative mt-6 grid grid-cols-6 gap-1" aria-label="Progress">
      <span aria-hidden="true" className="absolute top-3 right-[8.33%] left-[8.33%] h-0.5 bg-border">
        <span ref={bar} className="block h-full w-0 bg-primary" />
      </span>
      {STEPS.map(([key, label], i) => {
        const state = i < index || done ? 'done' : i === index ? 'now' : 'next';
        return (
          <li key={key} className="relative flex flex-col items-center text-center" aria-current={state === 'now' ? 'step' : undefined}>
            <span className={cn('grid size-6 place-items-center rounded-full border-2 bg-card text-[11px] font-semibold transition-colors',
              state === 'done' && 'border-primary bg-primary text-primary-foreground',
              state === 'now' && 'border-primary text-primary ring-4 ring-primary/20',
              state === 'next' && 'text-muted-foreground')}>
              {state === 'done' ? <Check className="size-3.5" aria-hidden="true" /> : i + 1}
            </span>
            <span className={cn('mt-2 hidden text-xs sm:block', state === 'next' ? 'text-muted-foreground' : 'text-foreground')}>{label}</span>
          </li>
        );
      })}
    </ol>
  );
}

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

  if (!t) return <PageLoading />;
  const plan = t.plan || {};
  const stepIndex = STEPS.findIndex(([, , statuses]) => statuses.includes(t.status));
  const prUrl = safeGitHubUrl(t.prUrl);

  return (
    <div className="space-y-6">
      <Link to="/tasks" className="inline-flex items-center gap-1 text-sm text-muted-foreground no-underline hover:text-foreground hover:no-underline">
        <ArrowLeft className="size-4" aria-hidden="true" /> All requests
      </Link>

      <section className="card">
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <Link to={`/repos/${encodeURIComponent(t.owner)}/${encodeURIComponent(t.name)}`}>{t.fullName}</Link>
          <span>· requested by @{t.requester}</span>
          <span className="flex-1" />
          <StatusPill status={t.status} />
        </div>
        <h1 className="mt-3">{firstLine(t.request)}</h1>
        {stepIndex >= 0 ? <Stepper index={stepIndex} done={t.status === 'completed'} /> : (
          <p className="mt-4 flex items-center gap-2 rounded-md border border-bad/30 bg-bad/10 px-3 py-2 text-sm text-bad">
            <CircleX className="size-4" aria-hidden="true" />{ENDED[t.status] || t.status}
          </p>
        )}
        {t.message && <p className="mt-4 text-sm text-muted-foreground">{t.message}</p>}
        {WORKING.has(t.status) && (
          <p className="mt-4 text-sm"><ShimmerText text="Working… this page updates by itself." className="text-sm font-medium" /></p>
        )}
      </section>

      {plan.summary && (
        <section className="card">
          <div className="flex flex-wrap items-center gap-3">
            <h2 className="flex-1">Plan</h2>
            {plan.risk && <span className={cn('tag', RISK[plan.risk])}>{plan.risk} risk</span>}
          </div>
          <div className="mt-2"><Markdown text={plan.summary} /></div>
          <dl className="mt-4 grid gap-x-6 gap-y-3 text-sm sm:grid-cols-[max-content_1fr]">
            {plan.risk_reason && <><dt className="text-muted-foreground">Why this risk</dt><dd>{plan.risk_reason}</dd></>}
            {plan.files?.length > 0 && (
              <><dt className="text-muted-foreground">Files</dt>
                <dd className="flex flex-wrap gap-1.5">{plan.files.map((f) => <code key={f} className="rounded bg-muted px-1.5 py-0.5">{f}</code>)}</dd></>
            )}
            {plan.how_to_verify && <><dt className="text-muted-foreground">How to verify</dt><dd>{plan.how_to_verify}</dd></>}
          </dl>
          {plan.acceptance_criteria?.length > 0 && (
            <>
              <h3>Acceptance criteria</h3>
              <ul className="space-y-1 text-sm">
                {plan.acceptance_criteria.map((c) => (
                  <li key={c} className="flex gap-2"><CircleCheck className="mt-0.5 size-4 shrink-0 text-ok" aria-hidden="true" />{c}</li>
                ))}
              </ul>
            </>
          )}
          {plan.questions?.length > 0 && (
            <>
              <h3>Questions from the agent</h3>
              <ul className="list-disc space-y-1 pl-5 text-sm">{plan.questions.map((q) => <li key={q}>{q}</li>)}</ul>
            </>
          )}
          {t.status === 'awaiting_plan_approval' && (
            <div className="mt-6 flex flex-wrap items-center gap-3 border-t pt-4">
              <button type="button" className="btn btn-primary" disabled={busy || !t.can.decidePlan}
                onClick={act(() => api.decidePlan(id, true))}>Approve plan</button>
              <button type="button" className="btn" disabled={busy || !t.can.rejectPlan}
                onClick={act(() => api.decidePlan(id, false))}>Reject</button>
              {t.can.why && <span className="text-xs text-muted-foreground">{t.can.why}</span>}
            </div>
          )}
          {t.can.clarify && (
            <div className="mt-6 border-t pt-4">
              <label htmlFor="answers" className="mb-1.5 block text-sm font-medium">Your answers</label>
              <textarea id="answers" className="field min-h-24" value={answers} maxLength={1500}
                onChange={(e) => setAnswers(e.target.value)} />
              <button type="button" className="btn btn-primary mt-3" disabled={busy || !answers.trim()} onClick={clarify}>
                Send answers and try again
              </button>
            </div>
          )}
        </section>
      )}

      {t.diff && (
        <section className="card">
          <h2>Proposed change</h2>
          {t.summary && <div className="mt-2"><Markdown text={t.summary} /></div>}
          <p className="mt-3 flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
            {(t.files || []).map((f) => <code key={f} className="rounded bg-muted px-1.5 py-0.5">{f}</code>)}
            <span>· diff sha256 <code>{(t.diffSha256 || '').slice(0, 12)}</code></span>
          </p>
          <div className="mt-3"><Diff text={t.diff} /></div>
          {t.diffTruncated && <p className="mt-2 text-xs text-muted-foreground">Diff truncated for display; the pull request has all of it.</p>}
          <h3 className="flex items-center gap-2">
            Checks {t.checksOk
              ? <span className="tag bg-ok/12 text-ok"><CircleCheck className="size-3" aria-hidden="true" />passed</span>
              : <span className="tag bg-bad/10 text-bad"><CircleX className="size-3" aria-hidden="true" />failed</span>}
          </h3>
          <pre className="code max-h-80">{t.checks}</pre>
          {t.status === 'awaiting_impl_approval' && (
            <div className="mt-6 flex flex-wrap items-center gap-3 border-t pt-4">
              <button type="button" className="btn btn-primary" disabled={busy || !t.can.decideChanges}
                onClick={act(() => api.decideChanges(id, true))}>
                <GitPullRequest className="size-4" aria-hidden="true" />
                {busy ? 'Opening pull request…' : 'Approve diff & open pull request'}
              </button>
              <button type="button" className="btn" disabled={busy || !t.can.decideChanges}
                onClick={act(() => api.decideChanges(id, false))}>Reject</button>
              {t.can.why && <span className="text-xs text-muted-foreground">{t.can.why}</span>}
            </div>
          )}
        </section>
      )}

      {t.status === 'completed' && (
        <section className="card border-ok/30 bg-ok/5">
          <p className="flex items-center gap-2 font-medium text-ok">
            <GitPullRequest className="size-5" aria-hidden="true" /> Pull request opened
          </p>
          {prUrl && (
            <a href={prUrl} target="_blank" rel="noreferrer noopener" className="mt-2 inline-flex items-center gap-1 text-sm">
              {prUrl} <ExternalLink className="size-3.5" aria-hidden="true" />
            </a>
          )}
          <p className="mt-3 text-sm text-muted-foreground">
            Nothing has been merged. An engineer reviews it on GitHub; the repository's branch protection decides who
            can approve and merge. The agent can't approve or merge its own pull requests.
          </p>
        </section>
      )}
    </div>
  );
}
