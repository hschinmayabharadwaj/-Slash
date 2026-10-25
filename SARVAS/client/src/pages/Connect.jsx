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
      <p className="muted">
        Ask questions about a repository, or describe a change in plain English. The agent plans it, makes it in an
        isolated sandbox, runs the checks and opens a pull request that an engineer must review and merge.
        SARVAS never asks for your GitHub password.
      </p>
      {/* Full-page navigation: the OAuth redirect must not be a fetch. */}
      <a className="btn primary" href="/auth/login">Connect GitHub</a>
    </section>
  );
}
