import { ArrowRight, Check, CircleAlert, FolderGit2, GitPullRequest, Loader } from 'lucide-react';
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

// GitHub adds this when it sends people back from the install page (the App's Setup URL).
const returnedFromGitHub = new URLSearchParams(window.location.search).get('setup_action');

const ROLE_HELP = {
  requester: 'You can ask questions and request changes. Engineers review and merge every pull request.',
  engineer: 'You can also approve medium-risk plans on repositories where you have push access.',
  admin: 'You can also see the full audit log and pause the agent.',
};
const ROLE_CLASS = { admin: 'bg-bad/10 text-bad', engineer: 'bg-primary/12 text-link', requester: '' };

const SLICES = [
  ['working', 'In progress', 'var(--chart-1)'],
  ['attention', 'Waiting on a person', 'var(--chart-4)'],
  ['done', 'Pull request opened', 'var(--chart-3)'],
  ['stopped', 'Stopped or failed', 'var(--chart-5)'],
];

export default function Dashboard({ onError, installUrl }) {
  const [me, setMe] = useState(null);
  const [repos, setRepos] = useState(null);
  const [tasks, setTasks] = useState(null);

  useEffect(() => {
    api.me().then(setMe).catch(onError);
    api.repos().then((d) => setRepos(d.repos)).catch(onError);
    api.tasks().then((d) => setTasks(d.tasks)).catch(onError);
    if (returnedFromGitHub) window.history.replaceState({}, '', '/');
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
              ? <span className="tag bg-ok/12 text-ok">Build tasks on</span>
              : <span className="tag">Build tasks off</span>}
          </p>
        </div>
      </section>

      {returnedFromGitHub && me.installations.length > 0 && (
        <p role="status" className="rounded-lg border border-ok/30 bg-ok/10 px-4 py-3 text-sm text-ok">
          GitHub saved your repository choice. They’re listed below.
        </p>
      )}

      {me.installations.length === 0 && <InstallApp login={me.login} installUrl={installUrl} />}

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
              <CountUp value={value} className="mt-4 block font-serif text-5xl font-medium leading-none tabular-nums text-foreground" />
              <span className="text-sm text-muted-foreground">{label}</span>
            </>
          );
          return to
            ? <Link key={label} to={to} className="card-cream block p-5 no-underline hover:no-underline active:bg-muted-strong sm:p-6">{body}</Link>
            : <div key={label} className="card-cream p-5 sm:p-6">{body}</div>;
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
        <Repos repos={repos} installUrl={installUrl} className="lg:col-span-2" />
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
          Revoke this app’s access to your account at any time from{' '}
          {safeGitHubUrl(me.revokeUrl) && <a href={me.revokeUrl} target="_blank" rel="noreferrer noopener">
            GitHub → Settings → Applications</a>}, or use Disconnect above.
        </p>
      </section>
    </Stagger>
  );
}

// First run: the App isn't installed anywhere this person can see yet.
function InstallApp({ login, installUrl }) {
  const steps = [
    [true, `Signed in as @${login}`],
    [false, 'Install the GitHub App and choose All repositories, or only the ones you pick'],
    [false, "You'll come straight back here with your repositories ready"],
  ];
  return (
    <section className="rounded-lg bg-primary p-8 text-primary-foreground sm:p-12">
      <h2 className="text-3xl text-primary-foreground sm:text-4xl">Connect your repositories</h2>
      <ol className="mt-6 space-y-3">
        {steps.map(([done, text], i) => (
          <li key={text} className="flex items-center gap-3">
            <span className="grid size-6 shrink-0 place-items-center rounded-full bg-primary-foreground/20 text-xs font-medium">
              {done ? <Check className="size-3.5" aria-hidden="true" /> : i + 1}
            </span>
            {text}
          </li>
        ))}
      </ol>
      {installUrl ? (
        // Full-page navigation: GitHub's own page grants the access, then returns via the App's Setup URL.
        <a href={installUrl} className="btn mt-8 h-11 border-transparent bg-background px-6 text-foreground active:bg-muted">
          Install the GitHub App <ArrowRight className="size-4" aria-hidden="true" />
        </a>
      ) : (
        <p className="mt-8 text-sm text-primary-foreground/90">
          Ask your administrator for the App's install link (they can show it here by setting GITHUB_APP_SLUG).
        </p>
      )}
    </section>
  );
}
