import { useEffect, useState } from 'react';
import { api } from '../api.js';
import { Link } from '../router.jsx';

export default function Tasks({ onError }) {
  const [tasks, setTasks] = useState(null);
  useEffect(() => { api.tasks().then((d) => setTasks(d.tasks)).catch(onError); }, [onError]);

  return (
    <section className="card">
      <h1>Change requests</h1>
      <p className="muted">Yours, plus (for engineers) every request on repositories you can push to.</p>
      <table>
        <thead><tr><th>Request</th><th>Repository</th><th>By</th><th>Status</th></tr></thead>
        <tbody>
          {tasks?.length === 0 && <tr><td colSpan={4} className="muted">Nothing yet. Open a repository to request a change.</td></tr>}
          {tasks?.map((t) => (
            <tr key={t.id}>
              <td><Link to={`/tasks/${encodeURIComponent(t.id)}`}>{t.request.split('\n')[0].slice(0, 80) || '(no text)'}</Link></td>
              <td>{t.fullName}</td>
              <td>@{t.requester}</td>
              <td><span className="tag">{t.status.replaceAll('_', ' ')}</span></td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
