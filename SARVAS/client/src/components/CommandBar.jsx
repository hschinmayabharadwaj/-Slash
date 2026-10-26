import { FolderGit2, History, LayoutDashboard, ListChecks, Shield, Wrench } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { api } from '@/api.js';
import ActionSearchBar from '@/components/kokonutui/action-search-bar';
import { firstLine } from '@/lib/status';
import { navigate } from '@/router.jsx';

const ICON = 'size-4';
const repoPath = (r) => `/repos/${encodeURIComponent(r.owner)}/${encodeURIComponent(r.name)}`;

// ⌘K / Ctrl+K palette: jump to any page, repository or recent task.
export default function CommandBar({ isAdmin, className }) {
  const [repos, setRepos] = useState([]);
  const [tasks, setTasks] = useState([]);

  // Best effort: pages report their own load errors.
  useEffect(() => {
    api.repos().then((d) => setRepos(d.repos)).catch(() => {});
    api.tasks().then((d) => setTasks(d.tasks)).catch(() => {});
  }, []);

  const items = useMemo(() => [
    { id: 'go-dashboard', label: 'Dashboard', icon: <LayoutDashboard className={ICON} />, group: 'Go to', to: '/' },
    { id: 'go-tasks', label: 'Change requests', icon: <ListChecks className={ICON} />, group: 'Go to', to: '/tasks', keywords: ['tasks'] },
    { id: 'go-activity', label: 'Your activity', icon: <History className={ICON} />, group: 'Go to', to: '/activity', keywords: ['audit', 'log'] },
    ...(isAdmin ? [{ id: 'go-admin', label: 'Administration', icon: <Shield className={ICON} />, group: 'Go to', to: '/admin', keywords: ['kill switch', 'audit'] }] : []),
    ...repos.map((r) => ({
      id: `repo-${r.fullName}`, label: r.fullName, icon: <FolderGit2 className={ICON} />, group: 'Repositories',
      to: repoPath(r), keywords: ['ask', 'build', 'readme'],
    })),
    ...tasks.slice(0, 8).map((t) => ({
      id: `task-${t.id}`, label: firstLine(t.request, 70), icon: <Wrench className={ICON} />, group: 'Recent requests',
      to: `/tasks/${encodeURIComponent(t.id)}`, keywords: [t.fullName],
    })),
  ], [isAdmin, repos, tasks]);

  return (
    <ActionSearchBar
      items={items}
      defaultRecent={[]}
      shortcut
      label="Search pages, repositories and requests"
      placeholder="Jump to…"
      onSelect={(item) => item.to && navigate(item.to)}
      className={className}
    />
  );
}
