import { useEffect, useState } from 'react';
import { api } from '../api.js';
import AuditTable from '../components/AuditTable.jsx';

export default function Activity({ onError }) {
  const [events, setEvents] = useState(null);
  useEffect(() => { api.activity().then((d) => setEvents(d.events)).catch(onError); }, [onError]);

  return (
    <section className="card">
      <h1>Your activity</h1>
      <p className="mt-1 text-sm text-muted-foreground">Audit log of everything done with your account through this page.</p>
      <AuditTable events={events} />
    </section>
  );
}
