const MESSAGES = {
  state: "The sign-in link expired or didn't come from this page. Please try again.",
  cancelled: 'GitHub authorization was cancelled.',
};

export default function Connect({ authError }) {
  return (
    <section className="card">
      <h1>Connect your GitHub account</h1>
      {authError && <p className="error">{MESSAGES[authError] ?? 'Sign-in failed.'}</p>}
      <p className="muted">
        The agent acts with <strong>your</strong> GitHub permissions, only on repositories where this app is
        installed. Every change is shown to you first and opened as a pull request. Nothing is pushed to your
        default branch.
      </p>
      <p className="muted">For now it can add or delete a <code>README.md</code>.</p>
      {/* Full-page navigation: the OAuth redirect must not be a fetch. */}
      <a className="btn primary" href="/auth/login">Connect GitHub</a>
    </section>
  );
}
