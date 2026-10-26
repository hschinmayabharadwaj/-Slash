import { animate } from 'animejs';
import { useEffect, useRef } from 'react';
import { reducedMotion } from '@/lib/utils';

const plain = (n) => Math.round(n).toLocaleString();

// Counts from the last shown value to `value` with anime.js, writing the text
// directly so React doesn't re-render every frame.
export default function CountUp({ value, format = plain, className }) {
  const ref = useRef(null);
  const shown = useRef(0);
  const fmt = useRef(format);
  fmt.current = format;

  useEffect(() => {
    const el = ref.current;
    if (reducedMotion()) {
      shown.current = value;
      el.textContent = fmt.current(value);
      return undefined;
    }
    const state = { n: shown.current };
    const anim = animate(state, {
      n: value,
      duration: 1100,
      ease: 'outExpo',
      onUpdate: () => {
        shown.current = state.n;
        el.textContent = fmt.current(state.n);
      },
    });
    return () => anim.pause();
  }, [value]);

  return <span ref={ref} className={className}>{format(0)}</span>;
}
