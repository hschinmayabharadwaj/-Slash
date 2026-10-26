import { createTimeline, stagger } from 'animejs';
import { ArrowRight, Check, GitPullRequest, KeyRound, MessageSquareCode, ShieldCheck, Wand2 } from 'lucide-react';
import { useEffect, useRef } from 'react';
import Logo, { GitHubMark } from '../components/Logo.jsx';
import Stagger from '../components/Stagger.jsx';
import { HoverBorderGradient } from '../components/ui/hover-border-gradient';
import { reducedMotion } from '../lib/utils';
import { Spotlight } from '../components/ui/spotlight-new';

const MESSAGES = {
  state: "The sign-in link expired or didn't come from this page. Please try again.",
  cancelled: 'GitHub authorization was cancelled.',
};

// Optional Spline scene, embedded through Spline's own viewer so its code runs
// on Spline's origin, never ours (the CSP allows only this frame host).
const SPLINE = import.meta.env.VITE_SPLINE_URL?.startsWith('https://my.spline.design/') ? import.meta.env.VITE_SPLINE_URL : null;

const FEATURES = [
  [MessageSquareCode, 'Ask the codebase', 'Plain-English answers with links to the exact lines they came from.'],
  [Wand2, 'Request a change', 'Describe what you want. The agent plans it and asks when something is unclear.'],
  [ShieldCheck, 'Sandboxed and checked', "Changes are made in an isolated container and run against your repository's checks."],
  [GitPullRequest, 'Always a pull request', 'Nothing touches your default branch. An engineer reviews and merges.'],
];

export default function Connect({ authError }) {
  return (
    <div className="relative min-h-dvh overflow-hidden">
      <Spotlight
        gradientFirst="radial-gradient(68.54% 68.72% at 55.02% 31.46%, hsla(250, 100%, 80%, .10) 0, hsla(250, 100%, 60%, .03) 50%, hsla(250, 100%, 50%, 0) 80%)"
        gradientSecond="radial-gradient(50% 50% at 50% 50%, hsla(250, 100%, 80%, .07) 0, hsla(250, 100%, 60%, .02) 80%, transparent 100%)"
        gradientThird="radial-gradient(50% 50% at 50% 50%, hsla(200, 100%, 80%, .05) 0, hsla(200, 100%, 50%, .02) 80%, transparent 100%)"
      />
      <div aria-hidden="true" className="pointer-events-none absolute inset-0 bg-[linear-gradient(to_right,var(--border)_1px,transparent_1px),linear-gradient(to_bottom,var(--border)_1px,transparent_1px)] bg-[size:56px_56px] opacity-40 [mask-image:radial-gradient(ellipse_at_top,black_20%,transparent_70%)]" />

      <header className="relative z-10 mx-auto flex max-w-6xl items-center gap-2 px-6 py-5 font-semibold">
        <Logo /> SARVAS
      </header>

      <Stagger className="relative z-10 mx-auto grid max-w-6xl items-center gap-12 px-6 pt-8 pb-16 lg:grid-cols-2 lg:pt-16">
        <div className="space-y-6">
          <span className="tag bg-card"><span className="size-1.5 rounded-full bg-ok" /> Self-hosted · Sandboxed · PR-only</span>
          <h1 className="text-4xl leading-[1.05] sm:text-5xl lg:text-6xl">
            An AI engineer for your repositories,{' '}
            <span className="bg-gradient-to-r from-[#8b7dff] to-[#38bdf8] bg-clip-text text-transparent">with you in charge.</span>
          </h1>
          <p className="max-w-xl text-lg text-muted-foreground">
            Ask questions about a repository, or describe a change in plain English. SARVAS plans it, builds it in an
            isolated sandbox, runs the checks and opens a pull request that an engineer must review and merge.
          </p>
          {authError && (
            <p role="alert" className="rounded-md border border-bad/30 bg-bad/10 px-3 py-2 text-sm text-bad">
              {MESSAGES[authError] ?? 'Sign-in failed.'}
            </p>
          )}
          <div className="flex flex-wrap items-center gap-4">
            {/* Full-page navigation: the OAuth redirect must not be a fetch. */}
            <HoverBorderGradient as="a" href="/auth/login" containerClassName="no-underline hover:no-underline"
              className="flex items-center gap-2 px-5 py-2.5 font-medium">
              <GitHubMark /> Connect GitHub <ArrowRight className="size-4" aria-hidden="true" />
            </HoverBorderGradient>
            <span className="flex items-center gap-1.5 text-sm text-muted-foreground">
              <KeyRound className="size-4" aria-hidden="true" /> Never asks for your password
            </span>
          </div>
        </div>
        {SPLINE ? (
          <iframe src={SPLINE} title="Decorative 3D scene" loading="lazy" referrerPolicy="no-referrer"
            sandbox="allow-scripts allow-same-origin" tabIndex={-1} aria-hidden="true"
            className="aspect-square w-full rounded-2xl border-0" />
        ) : <PipelinePreview />}
      </Stagger>

      <Stagger className="relative z-10 mx-auto grid max-w-6xl gap-4 px-6 pb-20 sm:grid-cols-2 lg:grid-cols-4">
        {FEATURES.map(([Icon, title, text]) => (
          <div key={title} className="card bg-card/70 backdrop-blur">
            <Icon className="mb-3 size-5 text-primary" aria-hidden="true" />
            <h2 className="text-base">{title}</h2>
            <p className="mt-1 text-sm text-muted-foreground">{text}</p>
          </div>
        ))}
      </Stagger>

      <footer className="relative z-10 border-t py-6 text-center text-xs text-muted-foreground">
        The agent acts with <strong>your</strong> GitHub permissions, only on repositories where this app is installed.
      </footer>
    </div>
  );
}

const STEPS = ['Plan the change', 'Approve the scope', 'Build in a sandbox', 'Run the checks', 'Open a pull request'];

// A looping illustration of one task moving through the pipeline (anime.js timeline).
function PipelinePreview() {
  const ref = useRef(null);
  useEffect(() => {
    if (reducedMotion()) return undefined;
    const q = (s) => ref.current.querySelectorAll(s);
    const tl = createTimeline({ loop: true, loopDelay: 1800, defaults: { ease: 'outCubic' } })
      .add(q('[data-check]'), { scale: [0, 1], opacity: [0, 1], duration: 400, delay: stagger(650) })
      .add(q('[data-bar]'), { width: ['0%', '100%'], duration: 650 * STEPS.length, ease: 'linear' }, 0)
      .add(q('[data-pr]'), { opacity: [0, 1], y: [10, 0], duration: 500 });
    return () => tl.revert();
  }, []);

  return (
    <div ref={ref} aria-hidden="true" className="card relative mx-auto w-full max-w-md bg-card/80 shadow-2xl shadow-primary/10 backdrop-blur">
      <p className="eyebrow">acme/storefront</p>
      <p className="mt-1 font-medium">Show the monthly price under the yearly plan</p>
      <div className="mt-4 h-1 overflow-hidden rounded-full bg-muted">
        <div data-bar className="h-full w-full rounded-full bg-gradient-to-r from-[#8b7dff] to-[#38bdf8]" />
      </div>
      <ol className="mt-5 space-y-3">
        {STEPS.map((s) => (
          <li key={s} className="flex items-center gap-3 text-sm">
            <span className="grid size-5 place-items-center rounded-full border bg-background">
              <Check data-check className="size-3 text-ok" />
            </span>
            {s}
          </li>
        ))}
      </ol>
      <div data-pr className="mt-5 flex items-center gap-2 rounded-md border border-ok/30 bg-ok/10 px-3 py-2 text-sm text-ok">
        <GitPullRequest className="size-4" /> Pull request #42 is ready for review
      </div>
    </div>
  );
}
