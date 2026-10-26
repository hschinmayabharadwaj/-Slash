import { ArrowLeft, ExternalLink, GitPullRequest } from 'lucide-react';
import { useEffect, useState } from 'react';
import { api } from '../api.js';
import Diff, { safeGitHubUrl } from '../components/Diff.jsx';
import { PageLoading } from '../components/Skeleton.jsx';
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

  if (!p) return <PageLoading />;
  const prUrl = safeGitHubUrl(p.prUrl);

  return (
    <div className="space-y-6">
      <Link to={`/repos/${encodeURIComponent(p.owner)}/${encodeURIComponent(p.name)}`}
        className="inline-flex items-center gap-1 text-sm text-muted-foreground no-underline hover:text-foreground hover:no-underline">
        <ArrowLeft className="size-4" aria-hidden="true" /> Back to {p.fullName}
      </Link>
      <section className="card">
        <p className="eyebrow">{p.fullName}</p>
        <h1 className="mt-1">{p.actionLabel}</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Base branch <code>{p.baseBranch}</code>. Approving creates a new branch with exactly this diff and opens a
          pull request. Your default branch is not touched.
        </p>
        <div className="mt-4"><Diff text={p.diff} /></div>

        {p.status === 'pending' && (
          <div className="mt-6 flex flex-wrap gap-3 border-t pt-4">
            <button type="button" className="btn btn-primary" disabled={busy} onClick={approve}>
              <GitPullRequest className="size-4" aria-hidden="true" />
              {busy ? 'Opening pull request…' : 'Approve & open pull request'}
            </button>
            <button type="button" className="btn" disabled={busy} onClick={discard}>Discard</button>
          </div>
        )}
        {p.status === 'done' && (
          <div className="mt-6 rounded-md border border-ok/30 bg-ok/10 px-4 py-3 text-sm">
            <p className="flex items-center gap-2 font-medium text-ok"><GitPullRequest className="size-4" aria-hidden="true" />Pull request opened</p>
            {prUrl
              ? <a href={prUrl} target="_blank" rel="noreferrer noopener" className="mt-1 inline-flex items-center gap-1">{prUrl}<ExternalLink className="size-3.5" aria-hidden="true" /></a>
              : <p className="mt-1">{p.prUrl}</p>}
          </div>
        )}
        {!['pending', 'done'].includes(p.status) && <p className="mt-4 text-sm text-bad">Status: {p.status}</p>}
      </section>
    </div>
  );
}
