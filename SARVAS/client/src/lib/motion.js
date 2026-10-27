import { gsap } from 'gsap';
import { ScrollTrigger } from 'gsap/ScrollTrigger';
import Lenis from 'lenis';
import 'lenis/dist/lenis.css';
import { reducedMotion } from './utils';

gsap.registerPlugin(ScrollTrigger);

let lenis = null;

// Smooth scrolling driven by GSAP's ticker so ScrollTrigger stays in sync.
// Skipped for people who ask for reduced motion.
export function startSmoothScroll() {
  if (lenis || reducedMotion()) return;
  lenis = new Lenis({
    // Inner scroll areas (code, palette results, textareas) keep native scrolling.
    prevent: (node) => Boolean(node.closest?.('pre, textarea, [role="listbox"], [data-lenis-prevent]')),
  });
  lenis.on('scroll', ScrollTrigger.update);
  gsap.ticker.add((t) => lenis.raf(t * 1000));
  gsap.ticker.lagSmoothing(0);
}

export function scrollToTop() {
  if (lenis) lenis.scrollTo(0, { immediate: true });
  else window.scrollTo(0, 0);
}

export { gsap, ScrollTrigger };
