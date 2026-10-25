import { useEffect, useState } from 'react';
import { api } from '../api.js';
import { Link } from '../router.jsx';

export default function Repos({ onError }) {
  const [repos, setRepos] = useState(null);
  useEffect(() => { api.repos().then((d) => setRepos(d.repos)).catch(onError); }, [onError]);

  return (
    <section className="card">
      <h1>Your repositories</h1>
      <p className="muted">
        Repositories where the app is installed and you have access. Don't see one? Install the GitHub App on it
        from your GitHub settings.
      </p>
      {repos === null && <p className="muted">Loading…</p>}
      {repos?.length === 0 && <p>No repositories yet.</p>}
      {repos?.length > 0 && (
        <ul className="repos">
          {repos.map((r) => (
            <li key={r.fullName}>
              <Link to={`/repos/${encodeURIComponent(r.owner)}/${encodeURIComponent(r.name)}`}>
                <span>{r.fullName}</span>
                <span>
                  <span className="tag">{r.access}</span>{' '}
                  <span className="tag">{r.private ? 'private' : 'public'}</span>
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
