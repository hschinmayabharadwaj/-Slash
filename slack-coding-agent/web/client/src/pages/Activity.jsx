import { useEffect, useState } from 'react';
import { api } from '../api.js';

export default function Activity({ onError }) {
  const [events, setEvents] = useState(null);
  useEffect(() => { api.activity().then((d) => setEvents(d.events)).catch(onError); }, [onError]);

  return (
    <section className="card">
      <h1>Your activity</h1>
      <p className="muted">Audit log of everything done with your account through this page.</p>
      <table>
        <thead><tr><th>When (UTC)</th><th>Event</th><th>Details</th></tr></thead>
        <tbody>
          {events?.length === 0 && <tr><td colSpan={3} className="muted">Nothing yet.</td></tr>}
          {events?.map((e, i) => (
            // eslint-disable-next-line react/no-array-index-key
            <tr key={i}>
              <td>{e.at.slice(0, 19).replace('T', ' ')}</td>
              <td>{e.event}</td>
              <td className="muted">
                {Object.entries(e.detail).map(([k, v]) => <div key={k}>{k}: {String(v)}</div>)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
