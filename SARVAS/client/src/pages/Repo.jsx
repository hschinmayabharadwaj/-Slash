import { ArrowLeft, FileText, FolderGit2, GitBranch, Lock, MessageSquareCode, RefreshCw, Wand2 } from 'lucide-react';
import { AnimatePresence, motion } from 'motion/react';
import { useCallback, useEffect, useState } from 'react';
import { api } from '../api.js';
import Markdown from '../components/Markdown.jsx';
import { PageLoading } from '../components/Skeleton.jsx';
import ShimmerText from '../components/kokonutui/shimmer-text';
import SmoothTab from '../components/kokonutui/smooth-tab';
import { PlaceholdersAndVanishInput } from '../components/ui/placeholders-and-vanish-input';
import { cn } from '../lib/utils';
import { Link, navigate } from '../router.jsx';

export default function Repo({ owner, name, onError }) {
  const [data, setData] = useState(null);
  const [tab, setTab] = useState('ask');

  useEffect(() => { api.repo(owner, name).then(setData).catch(onError); }, [owner, name, onError]);

  if (!data) return <PageLoading />;
  const { repo } = data;

  return (
    <div className="space-y-6">
      <Link to="/" className="inline-flex items-center gap-1 text-sm text-muted-foreground no-underline hover:text-foreground hover:no-underline">
        <ArrowLeft className="size-4" aria-hidden="true" /> Dashboard
      </Link>
      <header className="flex flex-wrap items-start gap-4">
        <span className="grid size-11 place-items-center rounded-lg border bg-card">
          <FolderGit2 className="size-5 text-primary" aria-hidden="true" />
        </span>
        <div className="min-w-0 flex-1">
          <h1 className="truncate"><span className="text-muted-foreground">{repo.owner}/</span>{repo.name}</h1>
          <p className="mt-1 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            <span className="tag"><GitBranch className="size-3" aria-hidden="true" /><code>{repo.defaultBranch}</code></span>
            <span className="tag">{repo.access} access</span>
            {repo.private && <span className="tag"><Lock className="size-3" aria-hidden="true" />private</span>}
          </p>
        </div>
      </header>
      <SmoothTab
        label="Repository tools"
        value={tab}
        onChange={setTab}
        items={[
          { id: 'ask', title: 'Ask', icon: MessageSquareCode, content: <Ask owner={owner} name={name} onError={onError} /> },
          { id: 'build', title: 'Request a change', icon: Wand2, content: <Build owner={owner} name={name} onError={onError} /> },
          { id: 'readme', title: 'README', icon: FileText, content: <Readme owner={owner} name={name} data={data} onError={onError} /> },
        ]}
      />
    </div>
  );
}

function IndexBar({ owner, name, onError }) {
  const [st, setSt] = useState(null);
  const load = useCallback(() => api.indexStatus(owner, name).then(setSt).catch(onError), [owner, name, onError]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (st?.state !== 'indexing') return undefined;
    const t = setTimeout(load, 2500);
    return () => clearTimeout(t);
  }, [st, load]);

  const reindex = () => api.reindex(owner, name).then(setSt).catch(onError);
  if (!st) return null;
  const dot = { ready: st.stale ? 'bg-warn' : 'bg-ok', indexing: 'bg-primary motion-safe:animate-pulse', missing: 'bg-muted-foreground' }[st.state];
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
      <span className={cn('size-2 rounded-full', dot)} aria-hidden="true" />
      {st.state === 'missing' && <span>Not indexed yet.</span>}
      {st.state === 'indexing' && <ShimmerText text="Reading the repository…" className="text-xs font-medium" />}
      {st.state === 'ready' && (
        <span>
          Indexed {st.files} files at <code>{st.commit.slice(0, 7)}</code>
          {st.stale ? ' · new commits since, refreshed automatically when you ask' : ' · up to date'}
        </span>
      )}
      {st.error && <span className="text-bad">{st.error}</span>}
      {st.state !== 'indexing' && (
        <button type="button" onClick={reindex} className="inline-flex items-center gap-1 text-primary hover:underline">
          <RefreshCw className="size-3" aria-hidden="true" />{st.state === 'missing' ? 'Index now' : 'Refresh'}
        </button>
      )}
    </div>
  );
}

const STATUS_NOTE = {
  outside_context: 'Outside the available context',
  needs_clarification: 'Needs clarification',
  low_confidence: 'Not confident enough to answer',
  indexing: 'Indexing',
};
const PLACEHOLDERS = [
  'Where is the checkout total calculated?',
  'How does sign-in work?',
  'Which files handle payments?',
  'What happens when a user signs up?',
  'Where are environment variables read?',
];

function Sources({ title, items }) {
  return (
    <>
      <h3>{title}</h3>
      <ul className="space-y-1 text-sm">
        {items.map((c) => (
          <li key={`${c.path}:${c.startLine}`}>
            <a href={c.url} target="_blank" rel="noreferrer noopener"><code>{c.path}</code> lines {c.startLine}–{c.endLine}</a>
          </li>
        ))}
      </ul>
    </>
  );
}

function Ask({ owner, name, onError }) {
  const [answers, setAnswers] = useState([]);
  const [busy, setBusy] = useState('');

  const ask = async (question) => {
    setBusy(question);
    try {
      const res = await api.ask(owner, name, question);
      setAnswers((a) => [{ id: Date.now(), question, ...res }, ...a]);
    } catch (err) { onError(err); } finally { setBusy(''); }
  };

  return (
    <div className="space-y-4">
      <div>
        <h2>Ask about this repository</h2>
        <p className="mt-1 text-sm text-muted-foreground">Answers cite the files and lines they come from.</p>
      </div>
      <IndexBar owner={owner} name={name} onError={onError} />
      <PlaceholdersAndVanishInput placeholders={PLACEHOLDERS} ariaLabel="Question" maxLength={2000}
        disabled={Boolean(busy)} onSubmit={(_, q) => ask(q)} />
      {busy && (
        <div className="rounded-md border border-dashed p-4">
          <p className="text-sm font-medium">{busy}</p>
          <ShimmerText text="Reading the code…" className="mt-1 text-sm font-medium" />
        </div>
      )}
      <AnimatePresence initial={false}>
        {answers.map((a) => (
          <motion.article key={a.id} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}
            className="rounded-md border bg-background p-4">
            <p className="font-medium">{a.question}</p>
            {STATUS_NOTE[a.status] && (
              <p className={cn('tag mt-2', a.status === 'outside_context' ? 'border-bad/30 text-bad' : '')}>{STATUS_NOTE[a.status]}</p>
            )}
            <div className="mt-2"><Markdown text={a.answer} /></div>
            {a.citations?.length > 0 && <Sources title="Sources" items={a.citations} />}
            {a.status !== 'answered' && a.sources?.length > 0 && <Sources title="Closest matches" items={a.sources.slice(0, 5)} />}
            {a.disclosure && (
              <p className="mt-3 text-xs text-muted-foreground">{a.disclosure}{a.commit && ` · commit ${a.commit.slice(0, 7)}`}</p>
            )}
          </motion.article>
        ))}
      </AnimatePresence>
    </div>
  );
}

const HOW = ['You describe it', 'Agent plans, you approve', 'Built and checked in a sandbox', 'You approve the diff', 'Engineer merges the PR'];

function Build({ owner, name, onError }) {
  const [request, setRequest] = useState('');
  const [criteria, setCriteria] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      const { id } = await api.createTask(owner, name, request, criteria);
      navigate(`/tasks/${encodeURIComponent(id)}`);
    } catch (err) { onError(err); } finally { setBusy(false); }
  };

  return (
    <div className="space-y-4">
      <div>
        <h2>Request a change</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Describe a feature, a bug or a UI change and how it should behave. The agent asks if anything is unclear.{' '}
          <strong className="text-foreground">An engineer must review and merge the pull request.</strong>
        </p>
      </div>
      <ol className="flex flex-wrap gap-2 text-xs">
        {HOW.map((s, i) => (
          <li key={s} className="tag"><span className="font-semibold text-primary">{i + 1}</span>{s}</li>
        ))}
      </ol>
      <form onSubmit={submit} className="space-y-4">
        <div>
          <label htmlFor="req" className="mb-1.5 block text-sm font-medium">What should change?</label>
          <textarea id="req" className="field min-h-28" value={request} maxLength={2000} onChange={(e) => setRequest(e.target.value)}
            placeholder="On the pricing page, the yearly plan should show the monthly equivalent price under it." />
          <p className="mt-1 text-right text-xs text-muted-foreground tabular-nums">{request.length}/2000</p>
        </div>
        <div>
          <label htmlFor="crit" className="mb-1.5 block text-sm font-medium">
            How will we know it's done? <span className="font-normal text-muted-foreground">(optional, one per line)</span>
          </label>
          <textarea id="crit" className="field min-h-24" value={criteria} maxLength={3000} onChange={(e) => setCriteria(e.target.value)}
            placeholder={'Yearly plan shows "$8/month billed yearly"\nMonthly plan is unchanged'} />
        </div>
        <button type="submit" className="btn btn-primary" disabled={busy || request.trim().length < 10}>
          <Wand2 className="size-4" aria-hidden="true" />{busy ? 'Starting…' : 'Start'}
        </button>
        {request.trim().length > 0 && request.trim().length < 10 && (
          <span className="ml-3 text-xs text-muted-foreground">A few more words, please.</span>
        )}
      </form>
    </div>
  );
}

function Readme({ owner, name, data, onError }) {
  const [content, setContent] = useState('');
  const [busy, setBusy] = useState('');
  const { repo, readme } = data;

  const run = (label, fn) => async () => {
    setBusy(label);
    try { await fn(); } catch (e) { onError(e); } finally { setBusy(''); }
  };
  const draft = run('draft', async () => setContent((await api.draft(owner, name)).markdown));
  const propose = (action) => run('propose', async () => {
    const { id } = await api.propose(owner, name, action, content);
    navigate(`/changes/${encodeURIComponent(id)}`);
  });

  if (repo.access === 'read') {
    return (
      <div>
        <h2>README</h2>
        <p className="mt-1 text-sm text-muted-foreground">Adding or deleting the README directly needs write access. Use “Request a change” instead.</p>
      </div>
    );
  }
  return readme.exists ? (
    <div className="space-y-4">
      <h2>README.md exists</h2>
      <pre className="code max-h-96">{readme.text.split('\n').slice(0, 40).join('\n')}</pre>
      <button type="button" className="btn btn-danger" disabled={Boolean(busy)} onClick={propose('delete_readme')}>
        Preview deleting README.md
      </button>
    </div>
  ) : (
    <div className="space-y-4">
      <div>
        <h2>Add a README.md</h2>
        <p className="mt-1 text-sm text-muted-foreground">Write it yourself, or let the AI draft one from the repository's files and edit it.</p>
      </div>
      {busy === 'draft'
        ? <div className="field grid min-h-72 place-items-center"><ShimmerText text="Drafting from the repository…" className="text-sm font-medium" /></div>
        : <textarea className="field min-h-72 font-mono text-[13px]" value={content} maxLength={50000} placeholder={`# ${repo.name}`}
            onChange={(e) => setContent(e.target.value)} aria-label="README content" />}
      <div className="flex flex-wrap gap-3">
        <button type="button" className="btn btn-primary" disabled={Boolean(busy) || !content.trim()} onClick={propose('create_readme')}>
          {busy === 'propose' ? 'Preparing…' : 'Preview change'}
        </button>
        <button type="button" className="btn" disabled={Boolean(busy)} onClick={draft}>
          <Wand2 className="size-4" aria-hidden="true" />Draft with AI
        </button>
      </div>
    </div>
  );
}
