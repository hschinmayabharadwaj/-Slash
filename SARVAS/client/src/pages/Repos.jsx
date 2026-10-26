import { ChevronRight, FolderGit2, Lock, Search } from 'lucide-react';
import { useState } from 'react';
import { cn } from '../lib/utils';
import { Link } from '../router.jsx';

export default function Repos({ repos, className }) {
  const [filter, setFilter] = useState('');
  const shown = (repos || []).filter((r) => r.fullName.toLowerCase().includes(filter.trim().toLowerCase()));

  return (
    <section className={cn('card', className)}>
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="flex-1">Your repositories</h2>
        {repos?.length > 5 && (
          <label className="relative">
            <span className="sr-only">Filter repositories</span>
            <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
            <input className="field w-48 py-1.5 pl-8" value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter…" />
          </label>
        )}
      </div>
      <p className="mt-1 text-sm text-muted-foreground">
        Where the app is installed and you have access. Missing one? Install the GitHub App on it from your GitHub settings.
      </p>
      {repos === null && <p className="mt-4 text-sm text-muted-foreground">Loading…</p>}
      {repos?.length === 0 && <p className="mt-4 text-sm">No repositories yet.</p>}
      {shown.length > 0 && (
        <ul className="mt-4 divide-y rounded-md border">
          {shown.map((r) => (
            <li key={r.fullName}>
              <Link to={`/repos/${encodeURIComponent(r.owner)}/${encodeURIComponent(r.name)}`}
                className="group flex items-center gap-3 px-3 py-2.5 text-foreground no-underline transition-colors hover:bg-muted hover:no-underline">
                <FolderGit2 className="size-4 text-muted-foreground" aria-hidden="true" />
                <span className="min-w-0 flex-1 truncate">
                  <span className="text-muted-foreground">{r.owner}/</span>{r.name}
                </span>
                {r.private && <Lock className="size-3.5 text-muted-foreground" aria-label="private" />}
                <span className="tag">{r.access}</span>
                <ChevronRight className="size-4 text-muted-foreground transition-transform group-hover:translate-x-0.5" aria-hidden="true" />
              </Link>
            </li>
          ))}
        </ul>
      )}
      {repos?.length > 0 && shown.length === 0 && <p className="mt-4 text-sm text-muted-foreground">No repository matches “{filter}”.</p>}
    </section>
  );
}
