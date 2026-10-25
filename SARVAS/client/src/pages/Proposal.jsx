import { useEffect, useState } from 'react';
import { api } from '../api.js';
import Diff, { safeGitHubUrl } from '../components/Diff.jsx';
import { Link, navigate } from '../router.jsx';

export default function Proposal({ id, onError }) {
  const [p, setP] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => { api.proposal(id).then(setP).catch(onError); }, [id, onError]);

  const approve = async () => {
    setBusy(true);
    try {
      const res = await api.approve(id);
      setP((cur) => ({ ...cur, status: res.status, prUrl: res.prUrl }));
    } catch (e) {
      onError(e);
      api.proposal(id).then(setP).catch(() => {});
    } finally {
      setBusy(false);
    }
  };
  const discard = async () => {
    try { await api.discard(id); navigate('/'); } catch (e) { onError(e); }
  };

  if (!p) return <p className="muted">Loading…</p>;
  const prUrl = safeGitHubUrl(p.prUrl);

  return (
    <>
      <section className="card">
        <h1>{p.actionLabel} — {p.fullName}</h1>
        <p className="muted">
          Base branch <code>{p.baseBranch}</code>. Approving creates a new branch with exactly this diff and opens a
          pull request. Your default branch is not touched.
        </p>
        <Diff text={p.diff} />

        {p.status === 'pending' && (
          <div className="row">
            <button type="button" className="btn primary" disabled={busy} onClick={approve}>
              {busy ? 'Opening pull request…' : 'Approve & open pull request'}
            </button>
            <button type="button" className="btn" disabled={busy} onClick={discard}>Discard</button>
          </div>
        )}
        {p.status === 'done' && (
          <p className="ok">
            ✓ Pull request opened:{' '}
            {prUrl ? <a href={prUrl} target="_blank" rel="noreferrer noopener">{prUrl}</a> : p.prUrl}
          </p>
        )}
        {!['pending', 'done'].includes(p.status) && <p className="error">Status: {p.status}</p>}
      </section>
      <p>
        <Link to={`/repos/${encodeURIComponent(p.owner)}/${encodeURIComponent(p.name)}`}>← Back to {p.fullName}</Link>
      </p>
    </>
  );
}
