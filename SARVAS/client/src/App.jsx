import { AlertCircle, History, LayoutDashboard, ListChecks, LogOut, Shield, X } from 'lucide-react';
import { AnimatePresence, motion } from 'motion/react';
import { Suspense, lazy, useCallback, useEffect, useState } from 'react';
import { api, getSession } from './api.js';
import { Link, match, navigate, usePath } from './router.jsx';
import CommandBar from './components/CommandBar.jsx';
import Logo from './components/Logo.jsx';
import { PageLoading } from './components/Skeleton.jsx';
import ShimmerText from './components/kokonutui/shimmer-text';
import { scrollToTop } from './lib/motion';
import { cn } from './lib/utils';

// One chunk per page, so sign-in doesn't download the dashboard's charts.
const Activity = lazy(() => import('./pages/Activity.jsx'));
const Admin = lazy(() => import('./pages/Admin.jsx'));
const Connect = lazy(() => import('./pages/Connect.jsx'));
const Dashboard = lazy(() => import('./pages/Dashboard.jsx'));
const Proposal = lazy(() => import('./pages/Proposal.jsx'));
const Repo = lazy(() => import('./pages/Repo.jsx'));
const Task = lazy(() => import('./pages/Task.jsx'));
const Tasks = lazy(() => import('./pages/Tasks.jsx'));

export default function App() {
  const path = usePath();
  const [session, setSession] = useState(null);
  const [error, setError] = useState('');

  const refresh = useCallback(() => getSession().then(setSession).catch((e) => setError(e.message)), []);
  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => { setError(''); scrollToTop(); }, [path]);

  // Shared error handler for pages: a signed-out response returns to Connect.
  const onError = useCallback((e) => {
    if (e.signedOut) refresh();
    setError(e.message);
  }, [refresh]);

  if (!session) {
    return (
      <div className="grid min-h-dvh place-items-center">
        {error ? <p className="text-bad" role="alert">{error}</p> : <ShimmerText text="SARVAS" className="text-2xl" />}
      </div>
    );
  }

  const query = new URLSearchParams(window.location.search);
  if (!session.signedIn) return <Suspense fallback={null}><Connect authError={query.get('error')} installUrl={session.installUrl} /></Suspense>;

  const route = match(path);
  const isAdmin = session.role === 'admin';
  let page;
  if (route.page === 'repo') page = <Repo key={path} owner={route.owner} name={route.name} onError={onError} />;
  else if (route.page === 'proposal') page = <Proposal key={path} id={route.id} actions={session.actions} onError={onError} />;
  else if (route.page === 'task') page = <Task key={path} id={route.id} onError={onError} />;
  else if (route.page === 'tasks') page = <Tasks onError={onError} />;
  else if (route.page === 'activity') page = <Activity onError={onError} />;
  else if (route.page === 'admin' && isAdmin) page = <Admin onError={onError} />;
  else page = <Dashboard onError={onError} installUrl={session.installUrl} />;

  const logout = async () => {
    try { await api.logout(); } finally { navigate('/'); refresh(); }
  };

  return (
    <div className="min-h-dvh">
      <Header path={path} login={session.login} isAdmin={isAdmin} onLogout={logout} />
      <main className="mx-auto max-w-6xl px-4 py-10 sm:px-6 sm:py-14">
        <AnimatePresence>
          {error && (
            <motion.div role="alert" initial={{ opacity: 0, y: -8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }}
              className="mb-6 flex items-start gap-3 rounded-lg border border-bad/30 bg-bad/10 px-4 py-3 text-sm">
              <AlertCircle className="mt-0.5 size-4 shrink-0 text-bad" aria-hidden="true" />
              <span className="flex-1">{error}</span>
              <button type="button" onClick={() => setError('')} aria-label="Dismiss" className="text-muted-foreground hover:text-foreground">
                <X className="size-4" />
              </button>
            </motion.div>
          )}
        </AnimatePresence>
        <Suspense fallback={<PageLoading />}>{page}</Suspense>
      </main>
    </div>
  );
}

function Header({ path, login, isAdmin, onLogout }) {
  const links = [
    ['/', 'Dashboard', LayoutDashboard],
    ['/tasks', 'Tasks', ListChecks],
    ['/activity', 'Activity', History],
    ...(isAdmin ? [['/admin', 'Admin', Shield]] : []),
  ];
  // Repository and README pages belong to the dashboard.
  const section = links.find(([to]) => to !== '/' && path.startsWith(to))?.[0] ?? '/';

  const nav = (pillId) => (
    <>
      {links.map(([to, label, Icon]) => (
        <Link key={to} to={to} className={cn('relative flex items-center gap-1.5 rounded-md px-3.5 py-2 text-sm font-medium no-underline hover:no-underline',
          section === to ? 'text-foreground' : 'text-muted-foreground hover:text-foreground')}>
          {section === to && (
            <motion.span layoutId={pillId} className="absolute inset-0 rounded-md bg-muted" transition={{ type: 'spring', bounce: 0.2, duration: 0.5 }} />
          )}
          <Icon className="relative size-4" aria-hidden="true" />
          <span className="relative">{label}</span>
        </Link>
      ))}
    </>
  );

  return (
    <header className="sticky top-0 z-40 border-b bg-background/90 backdrop-blur-md">
      <div className="mx-auto flex h-16 max-w-6xl items-center gap-3 px-4 sm:px-6">
        <Link to="/" className="flex items-center gap-2 font-medium text-foreground no-underline hover:no-underline">
          <Logo /> SARVAS
        </Link>
        <nav className="ml-3 hidden items-center gap-1 md:flex" aria-label="Main">{nav('pill')}</nav>
        <div className="flex-1" />
        <CommandBar isAdmin={isAdmin} className="hidden w-60 sm:block lg:w-72" />
        <span className="hidden text-sm text-muted-foreground lg:inline">@{login}</span>
        <button type="button" className="btn btn-ghost px-2" onClick={onLogout} title="Disconnect">
          <LogOut className="size-4" aria-hidden="true" /><span className="sr-only sm:not-sr-only">Disconnect</span>
        </button>
      </div>
      <nav className="mx-auto flex max-w-6xl gap-1 overflow-x-auto px-3 pb-2 md:hidden" aria-label="Main">{nav('pill-mobile')}</nav>
    </header>
  );
}
