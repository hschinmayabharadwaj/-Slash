import { useEffect, useState } from 'react';

// Minimal pushState router (no dependency).
export function navigate(to) {
  window.history.pushState({}, '', to);
  window.dispatchEvent(new PopStateEvent('popstate'));
}

export function usePath() {
  const [path, setPath] = useState(window.location.pathname);
  useEffect(() => {
    const onPop = () => setPath(window.location.pathname);
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);
  return path;
}

export function match(path) {
  let m;
  if ((m = path.match(/^\/repos\/([^/]+)\/([^/]+)\/?$/))) {
    return { page: 'repo', owner: decodeURIComponent(m[1]), name: decodeURIComponent(m[2]) };
  }
  if ((m = path.match(/^\/changes\/([^/]+)\/?$/))) return { page: 'proposal', id: decodeURIComponent(m[1]) };
  if (path === '/activity') return { page: 'activity' };
  return { page: 'home' };
}

export function Link({ to, children, className }) {
  return (
    <a href={to} className={className} onClick={(e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
      e.preventDefault();
      navigate(to);
    }}>{children}</a>
  );
}
