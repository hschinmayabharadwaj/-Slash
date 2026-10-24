import { useCallback, useEffect, useState } from 'react';
import { api, getSession } from './api.js';
import { Link, match, navigate, usePath } from './router.jsx';
import Activity from './pages/Activity.jsx';
import Connect from './pages/Connect.jsx';
import Proposal from './pages/Proposal.jsx';
import Repo from './pages/Repo.jsx';
import Repos from './pages/Repos.jsx';

export default function App() {
  const path = usePath();
  const [session, setSession] = useState(null);
  const [error, setError] = useState('');

  const refresh = useCallback(() => getSession().then(setSession).catch((e) => setError(e.message)), []);
  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => { setError(''); }, [path]);

  // Shared error handler for pages: a signed-out response returns to Connect.
  const onError = useCallback((e) => {
    if (e.signedOut) refresh();
    setError(e.message);
  }, [refresh]);

  if (!session) return <div className="loading">Loading…</div>;

  const route = match(path);
  const query = new URLSearchParams(window.location.search);
  let page;
  if (!session.signedIn) page = <Connect authError={query.get('error')} />;
  else if (route.page === 'repo') page = <Repo key={path} owner={route.owner} name={route.name} onError={onError} />;
  else if (route.page === 'proposal') page = <Proposal key={path} id={route.id} actions={session.actions} onError={onError} />;
  else if (route.page === 'activity') page = <Activity onError={onError} />;
  else page = <Repos onError={onError} />;

  const logout = async () => {
    try { await api.logout(); } finally { navigate('/'); refresh(); }
  };

  return (
    <>
      <header className="top">
        <Link to="/" className="brand">🤖 Coding Agent</Link>
        <span className="spacer" />
        {session.signedIn && (
          <>
            <Link to="/activity">Activity</Link>
            <span className="muted">@{session.login}</span>
            <button type="button" className="link" onClick={logout}>Disconnect</button>
          </>
        )}
      </header>
      <main>
        {error && <div className="banner" role="alert">{error}</div>}
        {page}
      </main>
    </>
  );
}
