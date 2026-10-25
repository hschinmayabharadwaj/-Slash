import { useEffect, useState } from 'react';
import { api } from '../api.js';

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
    <>
      <section className="card">
        <h1>Administration</h1>
        {me && (
          <div className="row">
            <span>{me.killSwitch ? 'The agent is paused: no new tasks, approvals or pull requests.' : 'The agent is running.'}</span>
            <button type="button" className={me.killSwitch ? 'btn primary' : 'btn danger'} onClick={toggle}>
              {me.killSwitch ? 'Resume the agent' : 'Pause the agent (kill switch)'}
            </button>
          </div>
        )}
      </section>
      <section className="card">
        <h2>Audit log (everyone)</h2>
        <p className="muted small">Requests, generated changes, reviews and approvals. No secrets are stored here.</p>
        <table>
          <thead><tr><th>When (UTC)</th><th>Who</th><th>Event</th><th>Details</th></tr></thead>
          <tbody>
            {events?.map((e, i) => (
              // eslint-disable-next-line react/no-array-index-key
              <tr key={i}>
                <td>{e.at.slice(0, 19).replace('T', ' ')}</td>
                <td>{e.login || '—'}</td>
                <td>{e.event}</td>
                <td className="muted">{Object.entries(e.detail).filter(([, v]) => v != null)
                  .map(([k, v]) => <div key={k}>{k}: {String(v)}</div>)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}
