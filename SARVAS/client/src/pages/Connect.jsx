import { ArrowRight, Check, GitPullRequest, KeyRound, MessageSquareCode, ShieldCheck, Wand2 } from 'lucide-react';
import { useLayoutEffect, useRef } from 'react';
import Logo, { GitHubMark } from '../components/Logo.jsx';
import Stagger from '../components/Stagger.jsx';
import { SplitText } from 'gsap/SplitText';
import { gsap } from '../lib/motion';
import { reducedMotion } from '../lib/utils';

gsap.registerPlugin(SplitText);

const MESSAGES = {
  state: "The sign-in link expired or didn't come from this page. Please try again.",
  cancelled: 'GitHub authorization was cancelled.',
  not_allowed: "Your GitHub account isn't on this SARVAS's list of allowed users. Ask an administrator to add your GitHub username.",
};

// Optional Spline scene, embedded through Spline's own viewer so its code runs
// on Spline's origin, never ours (the CSP allows only this frame host).
const SPLINE = import.meta.env.VITE_SPLINE_URL?.startsWith('https://my.spline.design/') ? import.meta.env.VITE_SPLINE_URL : null;

const FEATURES = [
  [MessageSquareCode, 'Ask the codebase', 'Plain-English answers that link to the exact lines they came from.'],
  [Wand2, 'Request a change', 'Describe what you want. The agent plans it and asks when something is unclear.'],
  [ShieldCheck, 'Sandboxed and checked', "Every change is made in an isolated container and run against your repository's checks."],
  [GitPullRequest, 'Always a pull request', 'Nothing touches your default branch. An engineer reviews and merges.'],
];

// Full-page navigation: the OAuth redirect must not be a fetch.
function ConnectButton({ className = 'btn-primary' }) {
  return (
    <a href="/auth/login" className={`btn ${className} h-11 px-6`}>
      <GitHubMark /> Connect GitHub <ArrowRight className="size-4" aria-hidden="true" />
    </a>
  );
}

export default function Connect({ authError, installUrl }) {
  const hero = useRef(null);

  // Headline rises in line by line; the rest follows.
  useLayoutEffect(() => {
    if (reducedMotion()) return undefined;
    const ctx = gsap.context(() => {
      const split = SplitText.create('[data-split]', { type: 'lines', mask: 'lines', aria: 'auto' });
      gsap.timeline({ defaults: { ease: 'power4.out' } })
        .from(split.lines, { yPercent: 110, duration: 1.1, stagger: 0.09 })
        .from('[data-fade]', { autoAlpha: 0, y: 18, duration: 0.8, stagger: 0.08 }, '-=0.7');
    }, hero);
    return () => ctx.revert();
  }, []);

  return (
    <div className="min-h-dvh bg-background">
      <header className="mx-auto flex h-16 max-w-[1200px] items-center gap-2 px-6 font-medium text-foreground">
        <Logo /> SARVAS
        <span className="flex-1" />
        <a href="/auth/login" className="btn btn-ghost hidden sm:inline-flex">Sign in</a>
      </header>

      <section ref={hero} className="mx-auto grid max-w-[1200px] items-center gap-12 px-6 py-16 lg:grid-cols-2 lg:py-24">
        <div>
          <h1 data-split className="display text-[2.5rem] leading-[1.05] tracking-[-0.03em] sm:text-6xl lg:text-[4.25rem]">
            Your repositories, with you in charge.
          </h1>
          <p data-fade className="mt-6 max-w-xl text-lg text-body">
            Describe a change; SARVAS plans, builds, tests and opens a pull request.
          </p>
          {authError && (
            <p data-fade role="alert" className="mt-6 rounded-md border border-bad/30 bg-bad/10 px-3.5 py-2.5 text-sm text-bad">
              {MESSAGES[authError] ?? 'Sign-in failed.'}
            </p>
          )}
          <div data-fade className="mt-8 flex flex-wrap items-center gap-5">
            <ConnectButton />
            {installUrl && <a href={installUrl} className="btn h-11 px-6">Install the GitHub App</a>}
            <span className="flex items-center gap-1.5 text-sm text-muted-foreground">
              <KeyRound className="size-4" aria-hidden="true" /> Never asks for your password
            </span>
          </div>
        </div>
        <div data-fade>
          {SPLINE ? (
            <iframe src={SPLINE} title="Decorative 3D scene" loading="lazy" referrerPolicy="no-referrer"
              sandbox="allow-scripts allow-same-origin" tabIndex={-1} aria-hidden="true"
              className="aspect-square w-full rounded-xl border-0" />
          ) : <PipelineWindow />}
        </div>
      </section>

      <section className="bg-muted py-24">
        <div className="mx-auto max-w-[1200px] px-6">
          <h2 className="display max-w-2xl text-4xl leading-[1.1] tracking-[-0.02em] sm:text-5xl">
            Every change is planned, checked and reviewed.
          </h2>
          <Stagger onScroll className="mt-12 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {FEATURES.map(([Icon, title, text]) => (
              <div key={title} className="rounded-lg bg-background p-8">
                <Icon className="size-5 text-primary" aria-hidden="true" />
                <p className="mt-5 text-lg font-medium text-foreground">{title}</p>
                <p className="mt-2 text-body">{text}</p>
              </div>
            ))}
          </Stagger>
        </div>
      </section>

      <Stagger onScroll className="mx-auto max-w-[1200px] px-6 py-24">
        <div className="rounded-lg bg-primary px-8 py-14 text-primary-foreground sm:px-16 sm:py-16">
          <h2 className="display max-w-2xl text-3xl leading-[1.15] tracking-[-0.01em] text-primary-foreground sm:text-4xl">
            Acts with your GitHub permissions, only where the app is installed.
          </h2>
          <p className="mt-4 max-w-xl text-primary-foreground/90">
            You approve the plan and the diff. An engineer merges the pull request. The agent can’t approve or merge its own work.
          </p>
          <div className="mt-8"><ConnectButton className="border-transparent bg-background text-foreground active:bg-muted" /></div>
        </div>
      </Stagger>

      <footer className="bg-dark py-16 text-on-dark-soft">
        <div className="mx-auto flex max-w-[1200px] flex-wrap items-center gap-3 px-6 text-sm">
          <span className="flex items-center gap-2 font-medium text-on-dark"><Logo /> SARVAS</span>
          <span className="flex-1" />
          <span>Self-hosted AI coding agent. Your code stays on your infrastructure unless you allow otherwise.</span>
        </div>
      </footer>
    </div>
  );
}

const STEPS = ['Plans the change', 'Waits for your approval', 'Builds it in a sandbox', "Runs the repository's checks", 'Opens a pull request'];

// The pipeline every request goes through, ticking along on a GSAP loop.
function PipelineWindow() {
  const ref = useRef(null);
  useLayoutEffect(() => {
    if (reducedMotion()) return undefined;
    const ctx = gsap.context(() => {
      gsap.timeline({ repeat: -1, repeatDelay: 1.8, defaults: { ease: 'power3.out' } })
        .fromTo('[data-bar]', { scaleX: 0 }, { scaleX: 1, duration: 0.65 * STEPS.length, ease: 'none' }, 0)
        .fromTo('[data-check]', { scale: 0, autoAlpha: 0 }, { scale: 1, autoAlpha: 1, duration: 0.4, stagger: 0.65 }, 0)
        .fromTo('[data-pr]', { autoAlpha: 0, y: 10 }, { autoAlpha: 1, y: 0, duration: 0.5 });
    }, ref);
    return () => ctx.revert();
  }, []);

  return (
    <div ref={ref} aria-hidden="true" className="card-dark mx-auto w-full max-w-lg rounded-xl">
      <p className="font-serif text-2xl leading-tight tracking-[-0.01em] text-on-dark">From your request to a pull request</p>
      <div className="mt-5 h-1 overflow-hidden rounded-full bg-dark-elevated">
        <div data-bar className="h-full origin-left rounded-full bg-primary" />
      </div>
      <ol className="mt-6 space-y-3">
        {STEPS.map((s) => (
          <li key={s} className="flex items-center gap-3 text-on-dark">
            <span className="grid size-5 place-items-center rounded-full bg-dark-elevated">
              <Check data-check className="size-3 text-chart-4" />
            </span>
            {s}
          </li>
        ))}
      </ol>
      <p data-pr className="mt-6 flex items-center gap-2 rounded-md bg-dark-elevated px-4 py-3 text-sm text-on-dark">
        <GitPullRequest className="size-4 text-primary" /> An engineer reviews and merges it on GitHub
      </p>
    </div>
  );
}
