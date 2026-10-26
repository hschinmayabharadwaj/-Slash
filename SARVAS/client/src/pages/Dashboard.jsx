import { CircleAlert, FolderGit2, GitPullRequest, Loader } from 'lucide-react';
import { useEffect, useState } from 'react';
import { api } from '../api.js';
import CountUp from '../components/CountUp.jsx';
import { safeGitHubUrl } from '../components/Diff.jsx';
import { PageLoading } from '../components/Skeleton.jsx';
import Stagger from '../components/Stagger.jsx';
import { Ring } from '../components/charts/ring';
import { RingCenter } from '../components/charts/ring-center';
import { RingChart } from '../components/charts/ring-chart';
import { taskStatus } from '../lib/status';
import { Link } from '../router.jsx';
import Repos from './Repos.jsx';

const ROLE_HELP = {
  requester: 'You can ask questions and request changes. Engineers review and merge every pull request.',
  engineer: 'You can also approve medium-risk plans on repositories where you have push access.',
  admin: 'You can also see the full audit log and pause the agent.',
};
const ROLE_CLASS = { admin: 'border-bad/30 text-bad', engineer: 'border-primary/30 text-primary', requester: '' };

const SLICES = [
  ['working', 'In progress', 'var(--chart-1)'],
  ['attention', 'Waiting on a person', 'var(--chart-4)'],
  ['done', 'Pull request opened', 'var(--chart-3)'],
  ['stopped', 'Stopped or failed', 'var(--chart-5)'],
];

export default function Dashboard({ onError }) {
  const [me, setMe] = useState(null);
  const [repos, setRepos] = useState(null);
  const [tasks, setTasks] = useState(null);

  useEffect(() => {
    api.me().then(setMe).catch(onError);
    api.repos().then((d) => setRepos(d.repos)).catch(onError);
    api.tasks().then((d) => setTasks(d.tasks)).catch(onError);
  }, [onError]);

  if (!me) return <PageLoading />;

  const count = (tone) => (tasks || []).filter((t) => {
    const s = taskStatus(t.status).tone;
    return tone === 'stopped' ? s === 'stopped' || s === 'failed' : s === tone;
  }).length;
  const total = tasks?.length ?? 0;
  const ring = SLICES.map(([tone, label, color]) => ({ label, value: count(tone), maxValue: Math.max(total, 1), color }));

  const stats = [
    [FolderGit2, 'Repositories', repos?.length ?? 0, null],
    [Loader, 'In progress', ring[0].value, '/tasks'],
    [CircleAlert, 'Waiting on a person', ring[1].value, '/tasks'],
    [GitPullRequest, 'Pull requests opened', ring[2].value, '/tasks'],
  ];

  return (
    <Stagger watch={me} className="space-y-6">
      <section className="flex flex-wrap items-center gap-4">
        {me.avatarUrl.startsWith('https://avatars.githubusercontent.com/') && (
          <img src={me.avatarUrl} alt="" className="size-14 rounded-full border" />
        )}
        <div className="min-w-0 flex-1">
          <h1>Welcome back, {me.name || me.login}</h1>
          <p className="mt-1 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            @{me.login}
            <span className={`tag ${ROLE_CLASS[me.role] || ''}`}>{me.role}</span>
            {me.build?.enabled
              ? <span className="tag border-ok/30 text-ok">Build tasks on</span>
              : <span className="tag">Build tasks off</span>}
          </p>
        </div>
      </section>

      {me.killSwitch && (
        <p role="status" className="rounded-lg border border-bad/30 bg-bad/10 px-4 py-3 text-sm text-bad">
          The agent is paused by an administrator. No new tasks, approvals or pull requests until it resumes.
        </p>
      )}

      <section className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {stats.map(([Icon, label, value, to]) => {
          const body = (
            <>
              <Icon className="size-4 text-muted-foreground" aria-hidden="true" />
              <CountUp value={value} className="mt-3 block text-3xl font-semibold tabular-nums text-foreground" />
              <span className="text-sm text-muted-foreground">{label}</span>
            </>
          );
          return to
            ? <Link key={label} to={to} className="card block p-4 no-underline transition-colors hover:border-primary/40 hover:no-underline sm:p-5">{body}</Link>
            : <div key={label} className="card p-4 sm:p-5">{body}</div>;
        })}
      </section>

      <div className="grid gap-6 lg:grid-cols-3">
        <section className="card lg:col-span-1">
          <h2>Change requests</h2>
          {tasks === null && <p className="mt-4 text-sm text-muted-foreground">Loading…</p>}
          {tasks?.length === 0 && (
            <p className="mt-4 text-sm text-muted-foreground">Nothing yet. Open a repository to ask a question or request a change.</p>
          )}
          {total > 0 && (
            <>
              <div className="mx-auto mt-4 max-w-[240px]">
                <RingChart data={ring} strokeWidth={10} ringGap={5} baseInnerRadius={52}>
                  {ring.map((d, i) => <Ring key={d.label} index={i} />)}
                  <RingCenter defaultLabel="Requests" />
                </RingChart>
              </div>
              <ul className="mt-4 space-y-1.5 text-sm">
                {ring.map((d) => (
                  <li key={d.label} className="flex items-center gap-2">
                    <span className="size-2.5 rounded-full" style={{ background: d.color }} />
                    <span className="flex-1 text-muted-foreground">{d.label}</span>
                    <span className="tabular-nums">{d.value}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>
        <Repos repos={repos} className="lg:col-span-2" />
      </div>

      <section className="card">
        <h2>Account and access</h2>
        <dl className="mt-4 grid gap-x-6 gap-y-3 text-sm sm:grid-cols-[max-content_1fr]">
          <dt className="text-muted-foreground">Verified email</dt>
          <dd>{me.emails === null ? <span className="muted">not shared with this app</span>
            : me.emails.length ? me.emails.join(', ') : <span className="muted">none verified</span>}</dd>
          <dt className="text-muted-foreground">Organizations</dt>
          <dd>{me.orgs.length ? me.orgs.join(', ') : <span className="muted">none</span>}</dd>
          <dt className="text-muted-foreground">App installed on</dt>
          <dd>{me.installations.map((i) => i.account).join(', ') || <span className="muted">nowhere yet</span>}</dd>
          <dt className="text-muted-foreground">Your role</dt>
          <dd>{ROLE_HELP[me.role]}</dd>
          <dt className="text-muted-foreground">AI model</dt>
          <dd>{me.ai.disclosure}</dd>
          <dt className="text-muted-foreground">Build tasks</dt>
          <dd>{me.build?.enabled ? 'Available'
            : <span className="muted">Off: {(me.build?.problems || []).join('; ')}</span>}</dd>
        </dl>
        <p className="mt-4 text-xs text-muted-foreground">
          Revoke this app's access to your account at any time from{' '}
          {safeGitHubUrl(me.revokeUrl) && <a href={me.revokeUrl} target="_blank" rel="noreferrer noopener">
            GitHub → Settings → Applications</a>}, or use Disconnect above.
        </p>
      </section>
    </Stagger>
  );
}
