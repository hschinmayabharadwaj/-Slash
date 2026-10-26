import { Pause, Play } from 'lucide-react';
import { useEffect, useState } from 'react';
import { api } from '../api.js';
import AuditTable from '../components/AuditTable.jsx';
import { cn } from '../lib/utils';

export default function Admin({ onError }) {
  const [events, setEvents] = useState(null);
  const [me, setMe] = useState(null);

  useEffect(() => {
    api.adminAudit().then((d) => setEvents(d.events)).catch(onError);
    api.me().then(setMe).catch(onError);
  }, [onError]);

  const toggle = async () => {
    try {
      const { killSwitch } = await api.killSwitch(!me.killSwitch);
      setMe((m) => ({ ...m, killSwitch }));
      setEvents((await api.adminAudit()).events);
    } catch (e) { onError(e); }
  };

  return (
    <div className="space-y-6">
      <h1>Administration</h1>
      {me && (
        <section className={cn('card flex flex-wrap items-center gap-4', me.killSwitch && 'border-bad/40 bg-bad/5')}>
          <span className="relative flex size-3">
            {!me.killSwitch && <span className="absolute inset-0 rounded-full bg-ok opacity-60 motion-safe:animate-ping" />}
            <span className={cn('relative size-3 rounded-full', me.killSwitch ? 'bg-bad' : 'bg-ok')} />
          </span>
          <div className="min-w-0 flex-1">
            <p className="font-medium">{me.killSwitch ? 'The agent is paused' : 'The agent is running'}</p>
            <p className="text-sm text-muted-foreground">
              {me.killSwitch ? 'No new tasks, approvals or pull requests until you resume it.' : 'The kill switch stops all new work immediately.'}
            </p>
          </div>
          <button type="button" className={me.killSwitch ? 'btn btn-primary' : 'btn btn-danger'} onClick={toggle}>
            {me.killSwitch
              ? <><Play className="size-4" aria-hidden="true" />Resume the agent</>
              : <><Pause className="size-4" aria-hidden="true" />Pause the agent</>}
          </button>
        </section>
      )}
      <section className="card">
        <h2>Audit log (everyone)</h2>
        <p className="mt-1 text-sm text-muted-foreground">Requests, generated changes, reviews and approvals. No secrets are stored here.</p>
        <AuditTable events={events} showWho />
      </section>
    </div>
  );
}
