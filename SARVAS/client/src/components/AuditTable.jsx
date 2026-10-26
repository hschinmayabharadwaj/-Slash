import { utc } from '@/lib/status';

// Shared by the personal activity page and the admin audit log.
export default function AuditTable({ events, showWho = false }) {
  if (!events) return <p className="mt-4 text-sm text-muted-foreground">Loading…</p>;
  if (!events.length) return <p className="mt-4 text-sm text-muted-foreground">Nothing yet.</p>;
  return (
    <div className="-mx-5 mt-4 overflow-x-auto px-5 sm:-mx-6 sm:px-6">
      <table className="table min-w-[560px]">
        <thead>
          <tr><th>When (UTC)</th>{showWho && <th>Who</th>}<th>Event</th><th>Details</th></tr>
        </thead>
        <tbody>
          {events.map((e, i) => (
            // eslint-disable-next-line react/no-array-index-key
            <tr key={i}>
              <td className="whitespace-nowrap font-mono text-xs text-muted-foreground">{utc(e.at)}</td>
              {showWho && <td>{e.login || '—'}</td>}
              <td><span className="tag font-mono">{e.event}</span></td>
              <td className="text-xs text-muted-foreground">
                {Object.entries(e.detail).filter(([, v]) => v != null)
                  .map(([k, v]) => <div key={k}><span className="text-foreground/70">{k}:</span> {String(v)}</div>)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
