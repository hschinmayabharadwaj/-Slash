import { useEffect, useState } from 'react';
import { api } from '../api.js';
import { safeGitHubUrl } from '../components/Diff.jsx';
import Repos from './Repos.jsx';

const ROLE_HELP = {
  requester: 'You can ask questions and request changes. Engineers review and merge every pull request.',
  engineer: 'You can also approve medium-risk plans on repositories where you have push access.',
  admin: 'You can also see the full audit log and pause the agent.',
};

export default function Dashboard({ onError }) {
  const [me, setMe] = useState(null);
  useEffect(() => { api.me().then(setMe).catch(onError); }, [onError]);

  return (
    <>
      <section className="card profile">
        {!me && <p className="muted">Loading your GitHub profile…</p>}
        {me && (
          <>
            <div className="who">
              {me.avatarUrl.startsWith('https://avatars.githubusercontent.com/') && <img src={me.avatarUrl} alt="" />}
              <div>
                <h1>{me.name || me.login}</h1>
                <p className="muted">
                  @{me.login} · <span className={`role ${me.role}`}>{me.role}</span>
                </p>
              </div>
            </div>
            <dl className="facts">
              <dt>Verified email</dt>
              <dd>{me.emails === null ? <span className="muted">not shared with this app</span>
                : me.emails.length ? me.emails.join(', ') : <span className="muted">none verified</span>}</dd>
              <dt>Organizations</dt>
              <dd>{me.orgs.length ? me.orgs.join(', ') : <span className="muted">none</span>}</dd>
              <dt>App installed on</dt>
              <dd>{me.installations.map((i) => i.account).join(', ') || <span className="muted">nowhere yet</span>}</dd>
              <dt>Your role</dt>
              <dd>{ROLE_HELP[me.role]}</dd>
              <dt>AI model</dt>
              <dd>{me.ai.disclosure}</dd>
              <dt>Build tasks</dt>
              <dd>{me.build?.enabled ? 'Available'
                : <span className="muted">Off: {(me.build?.problems || []).join('; ')}</span>}</dd>
            </dl>
            {me.killSwitch && <p className="error">The agent is paused by an administrator.</p>}
            <p className="muted small">
              Revoke this app's access to your account at any time from{' '}
              {safeGitHubUrl(me.revokeUrl) && <a href={me.revokeUrl} target="_blank" rel="noreferrer noopener">
                GitHub → Settings → Applications</a>}, or use Disconnect above.
            </p>
          </>
        )}
      </section>
      <Repos onError={onError} />
    </>
  );
}
