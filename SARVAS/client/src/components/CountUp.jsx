import { useEffect, useRef } from 'react';
import { gsap } from '@/lib/motion';
import { reducedMotion } from '@/lib/utils';

const plain = (n) => Math.round(n).toLocaleString();

// Counts from the last shown value to `value` (GSAP), writing the text
// directly so React doesn't re-render every frame.
export default function CountUp({ value, format = plain, className }) {
  const ref = useRef(null);
  const state = useRef({ n: 0 });
  const fmt = useRef(format);
  fmt.current = format;

  useEffect(() => {
    const el = ref.current;
    const draw = () => { el.textContent = fmt.current(state.current.n); };
    if (reducedMotion()) {
      state.current.n = value;
      draw();
      return undefined;
    }
    const tween = gsap.to(state.current, { n: value, duration: 1.1, ease: 'expo.out', onUpdate: draw });
    return () => tween.kill();
  }, [value]);

  return <span ref={ref} className={className}>{format(0)}</span>;
}
