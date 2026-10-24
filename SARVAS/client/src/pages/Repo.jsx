import { useEffect, useState } from 'react';
import { api } from '../api.js';
import { Link, navigate } from '../router.jsx';

export default function Repo({ owner, name, onError }) {
  const [data, setData] = useState(null);
  const [content, setContent] = useState('');
  const [busy, setBusy] = useState('');

  useEffect(() => { api.repo(owner, name).then(setData).catch(onError); }, [owner, name, onError]);

  const run = (label, fn) => async () => {
    setBusy(label);
    try { await fn(); } catch (e) { onError(e); } finally { setBusy(''); }
  };
  const draft = run('draft', async () => setContent((await api.draft(owner, name)).markdown));
  const propose = (action) => run('propose', async () => {
    const { id } = await api.propose(owner, name, action, content);
    navigate(`/changes/${encodeURIComponent(id)}`);
  });

  if (!data) return <p className="muted">Loading…</p>;
  const { repo, readme } = data;

  return (
    <>
      <section className="card">
        <h1>{repo.fullName}</h1>
        <p className="muted">Default branch <code>{repo.defaultBranch}</code> · you have write access</p>
      </section>

      {readme.exists ? (
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
      )}
      <p><Link to="/">← All repositories</Link></p>
    </>
  );
}
