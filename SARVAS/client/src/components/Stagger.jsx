import { useLayoutEffect, useRef } from 'react';
import { gsap } from '@/lib/motion';
import { reducedMotion } from '@/lib/utils';

// Lifts the direct children in one after another (GSAP) whenever `watch`
// changes, e.g. when a page's data arrives. With `onScroll`, each child waits
// until it scrolls into view.
export default function Stagger({ watch, onScroll = false, className, children }) {
  const ref = useRef(null);
  useLayoutEffect(() => {
    const els = [...(ref.current?.children || [])];
    if (!els.length || reducedMotion()) return undefined;
    const ctx = gsap.context(() => {
      const to = { autoAlpha: 1, y: 0, duration: 0.8, ease: 'power3.out' };
      if (onScroll) {
        els.forEach((el) => gsap.fromTo(el, { autoAlpha: 0, y: 32 }, { ...to, scrollTrigger: { trigger: el, start: 'top 88%' } }));
      } else {
        gsap.fromTo(els, { autoAlpha: 0, y: 16 }, { ...to, stagger: 0.07 });
      }
    }, ref);
    return () => ctx.revert();
  }, [watch, onScroll]);
  return <div ref={ref} className={className}>{children}</div>;
}
