import { useCallback, useEffect, useState } from 'react';
import { api, getSession } from './api.js';
import { Link, match, navigate, usePath } from './router.jsx';
import Activity from './pages/Activity.jsx';
import Admin from './pages/Admin.jsx';
import Connect from './pages/Connect.jsx';
import Dashboard from './pages/Dashboard.jsx';
import Proposal from './pages/Proposal.jsx';
import Repo from './pages/Repo.jsx';
import Task from './pages/Task.jsx';
import Tasks from './pages/Tasks.jsx';

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
  else if (route.page === 'task') page = <Task key={path} id={route.id} onError={onError} />;
  else if (route.page === 'tasks') page = <Tasks onError={onError} />;
  else if (route.page === 'activity') page = <Activity onError={onError} />;
  else if (route.page === 'admin' && session.role === 'admin') page = <Admin onError={onError} />;
  else page = <Dashboard onError={onError} />;

  const logout = async () => {
    try { await api.logout(); } finally { navigate('/'); refresh(); }
  };

  return (
    <>
      <header className="top">
        <Link to="/" className="brand">🤖 SARVAS</Link>
        <span className="spacer" />
        {session.signedIn && (
          <>
            <Link to="/tasks">Tasks</Link>
            <Link to="/activity">Activity</Link>
            {session.role === 'admin' && <Link to="/admin">Admin</Link>}
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
