import { animate, stagger, utils } from 'animejs';
import { useLayoutEffect, useRef } from 'react';
import { reducedMotion } from '@/lib/utils';

// Lifts the direct children in one after another (anime.js) whenever `watch`
// changes, e.g. when a page's data arrives.
export default function Stagger({ watch, className, children }) {
  const ref = useRef(null);
  useLayoutEffect(() => {
    const els = [...(ref.current?.children || [])];
    if (!els.length || reducedMotion()) return undefined;
    utils.set(els, { opacity: 0, y: 14 });
    const anim = animate(els, { opacity: 1, y: 0, duration: 650, delay: stagger(70), ease: 'outCubic' });
    return () => anim.revert();
  }, [watch]);
  return <div ref={ref} className={className}>{children}</div>;
}
