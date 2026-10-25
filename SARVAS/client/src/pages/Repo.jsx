import { useCallback, useEffect, useState } from 'react';
import { api } from '../api.js';
import Markdown from '../components/Markdown.jsx';
import { Link, navigate } from '../router.jsx';

const TABS = [['ask', 'Ask'], ['build', 'Request a change'], ['readme', 'README']];

export default function Repo({ owner, name, onError }) {
  const [data, setData] = useState(null);
  const [tab, setTab] = useState('ask');

  useEffect(() => { api.repo(owner, name).then(setData).catch(onError); }, [owner, name, onError]);

  if (!data) return <p className="muted">Loading…</p>;
  const { repo } = data;

  return (
    <>
      <section className="card">
        <h1>{repo.fullName}</h1>
        <p className="muted">
          Default branch <code>{repo.defaultBranch}</code> · you have <strong>{repo.access}</strong> access
          {repo.private && ' · private'}
        </p>
        <nav className="tabs" role="tablist">
          {TABS.map(([id, label]) => (
            <button key={id} type="button" role="tab" aria-selected={tab === id}
              className={tab === id ? 'tab active' : 'tab'} onClick={() => setTab(id)}>{label}</button>
          ))}
        </nav>
      </section>
      {tab === 'ask' && <Ask owner={owner} name={name} onError={onError} />}
      {tab === 'build' && <Build owner={owner} name={name} onError={onError} />}
      {tab === 'readme' && <Readme owner={owner} name={name} data={data} onError={onError} />}
      <p><Link to="/">← Dashboard</Link></p>
    </>
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
  return (
    <div className="indexbar muted small">
      {st.state === 'missing' && <>Not indexed yet. </>}
      {st.state === 'indexing' && <>Reading the repository… </>}
      {st.state === 'ready' && (
        <>
          Indexed {st.files} files at <code>{st.commit.slice(0, 7)}</code>
          {st.stale ? ' (new commits since — refreshed automatically when you ask)' : ' (up to date)'}.{' '}
        </>
      )}
      {st.error && <span className="error">{st.error} </span>}
      {st.state !== 'indexing' && (
        <button type="button" className="link" onClick={reindex}>{st.state === 'missing' ? 'Index now' : 'Refresh'}</button>
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

function Ask({ owner, name, onError }) {
  const [question, setQuestion] = useState('');
  const [answers, setAnswers] = useState([]);
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    if (!question.trim()) return;
    setBusy(true);
    try {
      const res = await api.ask(owner, name, question);
      setAnswers((a) => [{ question, ...res }, ...a]);
      if (res.status !== 'indexing') setQuestion('');
    } catch (err) { onError(err); } finally { setBusy(false); }
  };

  return (
    <section className="card">
      <h2>Ask about this repository</h2>
      <IndexBar owner={owner} name={name} onError={onError} />
      <form onSubmit={submit} className="row">
        <input className="grow" value={question} maxLength={2000} onChange={(e) => setQuestion(e.target.value)}
          placeholder="e.g. Where is the checkout total calculated?" aria-label="Question" />
        <button type="submit" className="btn primary" disabled={busy || !question.trim()}>
          {busy ? 'Thinking…' : 'Ask'}
        </button>
      </form>
      {answers.map((a, i) => (
        // eslint-disable-next-line react/no-array-index-key
        <article key={i} className={`answer ${a.status}`}>
          <p className="q">{a.question}</p>
          {STATUS_NOTE[a.status] && <p className="note">{STATUS_NOTE[a.status]}</p>}
          <Markdown text={a.answer} />
          {a.citations?.length > 0 && (
            <>
              <h3>Sources</h3>
              <ul className="cites">
                {a.citations.map((c) => (
                  <li key={`${c.path}:${c.startLine}`}>
                    <a href={c.url} target="_blank" rel="noreferrer noopener"><code>{c.path}</code> lines {c.startLine}–{c.endLine}</a>
                  </li>
                ))}
              </ul>
            </>
          )}
          {a.status !== 'answered' && a.sources?.length > 0 && (
            <>
              <h3>Closest matches</h3>
              <ul className="cites">
                {a.sources.slice(0, 5).map((c) => (
                  <li key={`${c.path}:${c.startLine}`}>
                    <a href={c.url} target="_blank" rel="noreferrer noopener"><code>{c.path}</code> lines {c.startLine}–{c.endLine}</a>
                  </li>
                ))}
              </ul>
            </>
          )}
          {a.disclosure && <p className="muted small">{a.disclosure}{a.commit && ` · commit ${a.commit.slice(0, 7)}`}</p>}
        </article>
      ))}
    </section>
  );
}

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
    <section className="card">
      <h2>Request a change</h2>
      <p className="muted">
        Describe what you want in plain English: a feature, a bug, a UI change, and how it should behave. The agent
        reads the code, asks questions if anything is unclear, and shows you a plan. After you approve it, the
        agent makes the change in an isolated sandbox, runs the repository's checks and shows you the diff. Approving the
        diff opens a pull request. <strong>An engineer must review and merge it</strong>.
      </p>
      <form onSubmit={submit}>
        <label htmlFor="req">What should change?</label>
        <textarea id="req" className="short" value={request} maxLength={2000} onChange={(e) => setRequest(e.target.value)}
          placeholder="On the pricing page, the yearly plan should show the monthly equivalent price under it." />
        <label htmlFor="crit">How will we know it's done? <span className="muted">(optional, one per line)</span></label>
        <textarea id="crit" className="short" value={criteria} maxLength={3000} onChange={(e) => setCriteria(e.target.value)}
          placeholder={'Yearly plan shows "$8/month billed yearly"\nMonthly plan is unchanged'} />
        <div className="row">
          <button type="submit" className="btn primary" disabled={busy || request.trim().length < 10}>
            {busy ? 'Starting…' : 'Start'}
          </button>
        </div>
      </form>
    </section>
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
      <section className="card">
        <h2>README</h2>
        <p className="muted">Adding or deleting the README directly needs write access. Use “Request a change” instead.</p>
      </section>
    );
  }
  return readme.exists ? (
    <section className="card">
      <h2>README.md exists</h2>
      <pre className="code">{readme.text.split('\n').slice(0, 40).join('\n')}</pre>
      <div className="row">
        <button type="button" className="btn danger" disabled={Boolean(busy)} onClick={propose('delete_readme')}>
          Preview deleting README.md
        </button>
      </div>
    </section>
  ) : (
    <section className="card">
      <h2>Add a README.md</h2>
      <p className="muted">Write it yourself, or let the AI draft one from the repository's files and edit it.</p>
      <textarea value={content} maxLength={50000} placeholder={`# ${repo.name}`}
        onChange={(e) => setContent(e.target.value)} aria-label="README content" />
      <div className="row">
        <button type="button" className="btn primary" disabled={Boolean(busy) || !content.trim()}
          onClick={propose('create_readme')}>
          {busy === 'propose' ? 'Preparing…' : 'Preview change'}
        </button>
        <button type="button" className="btn" disabled={Boolean(busy)} onClick={draft}>
          {busy === 'draft' ? 'Drafting…' : 'Draft with AI'}
        </button>
      </div>
    </section>
  );
}
