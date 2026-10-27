import { ChevronRight, Inbox } from 'lucide-react';
import { useEffect, useState } from 'react';
import { api } from '../api.js';
import { PageLoading } from '../components/Skeleton.jsx';
import Stagger from '../components/Stagger.jsx';
import StatusPill from '../components/StatusPill.jsx';
import { firstLine, taskStatus } from '../lib/status';
import { cn } from '../lib/utils';
import { Link } from '../router.jsx';

const FILTERS = [
  ['all', 'All', () => true],
  ['attention', 'Waiting on a person', (tone) => tone === 'attention'],
  ['working', 'In progress', (tone) => tone === 'working'],
  ['done', 'Pull request opened', (tone) => tone === 'done'],
  ['stopped', 'Stopped', (tone) => tone === 'stopped' || tone === 'failed'],
];

export default function Tasks({ onError }) {
  const [tasks, setTasks] = useState(null);
  const [filter, setFilter] = useState('all');
  useEffect(() => { api.tasks().then((d) => setTasks(d.tasks)).catch(onError); }, [onError]);

  if (!tasks) return <PageLoading />;
  const keep = FILTERS.find(([id]) => id === filter)[2];
  const shown = tasks.filter((t) => keep(taskStatus(t.status).tone));

  return (
    <div className="space-y-6">
      <div>
        <h1>Change requests</h1>
        <p className="mt-1 text-sm text-muted-foreground">Yours, plus (for engineers) every request on repositories you can push to.</p>
      </div>
      <div className="flex flex-wrap gap-2" role="group" aria-label="Filter by status">
        {FILTERS.map(([id, label, test]) => (
          <button key={id} type="button" aria-pressed={filter === id} onClick={() => setFilter(id)}
            className={cn('inline-flex items-center gap-2 rounded-md px-3.5 py-2 text-sm font-medium',
              filter === id ? 'bg-muted-strong text-foreground' : 'text-muted-foreground')}>
            {label}
            <span className="tabular-nums opacity-70">{tasks.filter((t) => test(taskStatus(t.status).tone)).length}</span>
          </button>
        ))}
      </div>
      {shown.length === 0 ? (
        <div className="card flex flex-col items-center py-12 text-center">
          <Inbox className="size-8 text-muted-foreground" aria-hidden="true" />
          <p className="mt-3 font-medium">Nothing here</p>
          <p className="text-sm text-muted-foreground">
            {tasks.length ? 'No request matches this filter.' : 'Open a repository to request a change.'}
          </p>
        </div>
      ) : (
        <Stagger watch={`${filter}:${tasks.length}`} className="card divide-y p-0 sm:p-0">
          {shown.map((t) => (
            <Link key={t.id} to={`/tasks/${encodeURIComponent(t.id)}`}
              className="group flex items-center gap-4 px-4 py-3.5 text-foreground no-underline transition-colors first:rounded-t-lg last:rounded-b-lg hover:bg-muted hover:no-underline sm:px-5">
              <div className="min-w-0 flex-1">
                <p className="truncate font-medium">{firstLine(t.request, 100)}</p>
                <p className="mt-0.5 truncate text-xs text-muted-foreground">{t.fullName} · @{t.requester}</p>
              </div>
              <StatusPill status={t.status} className="hidden sm:inline-flex" />
              <ChevronRight className="size-4 shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5" aria-hidden="true" />
            </Link>
          ))}
        </Stagger>
      )}
    </div>
  );
}
