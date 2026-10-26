"use client";;
import { useEffect, useLayoutEffect, useRef, useState } from "react";

/** `useLayoutEffect` in the browser, `useEffect` on the server (no SSR warning). */
export const useIsomorphicLayoutEffect =
  typeof window === "undefined" ? useEffect : useLayoutEffect;

const ZERO_SIZE = { width: 0, height: 0 };

/**
 * Layout size of `element`, unaffected by CSS transforms. Fractional like
 * `ResizeObserver`'s `contentRect`, so the observer's first notification
 * matches and doesn't trigger a second render.
 */
function readElementSize(element) {
  const style = getComputedStyle(element);
  const width = Number.parseFloat(style.width);
  const height = Number.parseFloat(style.height);
  return {
    width: Number.isFinite(width) ? width : element.clientWidth,
    height: Number.isFinite(height) ? height : element.clientHeight,
  };
}

/**
 * Tracks an element's size. The first measurement happens in a layout effect,
 * so the sized render commits before the browser paints its first frame
 * (no blank frame while a `ResizeObserver` round-trips). Later changes come
 * from a `ResizeObserver`; `debounceTime` coalesces bursts after an
 * immediate leading update, like visx `useParentSize`.
 */
export function useElementSize(ref, debounceTime = 0) {
  const [size, setSize] = useState(ZERO_SIZE);

  useIsomorphicLayoutEffect(() => {
    const element = ref.current;
    if (!element) {
      return;
    }
    const apply = () => {
      const next = readElementSize(element);
      setSize((prev) =>
        prev.width === next.width && prev.height === next.height ? prev : next
      );
    };
    apply();

    if (typeof ResizeObserver === "undefined") {
      return;
    }
    let timeout = null;
    let trailing = false;
    const observer = new ResizeObserver(() => {
      if (debounceTime <= 0) {
        apply();
        return;
      }
      if (timeout !== null) {
        trailing = true;
        return;
      }
      apply();
      const flush = () => {
        if (trailing) {
          trailing = false;
          apply();
          timeout = setTimeout(flush, debounceTime);
          return;
        }
        timeout = null;
      };
      timeout = setTimeout(flush, debounceTime);
    });
    observer.observe(element);
    return () => {
      observer.disconnect();
      if (timeout !== null) {
        clearTimeout(timeout);
      }
    };
  }, [ref, debounceTime]);

  return size;
}

/**
 * Drop-in for visx `<ParentSize>` without its `overflow: hidden` measuring
 * layer, so tooltips, markers, and strokes at the chart edge aren't clipped.
 * Measures before first paint (see {@link useElementSize}).
 */
export function ParentSize({
  children,
  className,
  debounceTime,
  style
}) {
  const parentRef = useRef(null);
  const { width, height } = useElementSize(parentRef, debounceTime ?? 300);
  return (
    <div
      className={className}
      style={{ position: "relative", width: "100%", height: "100%", ...style }}
    >
      <div ref={parentRef} style={{ position: "absolute", inset: 0 }}>
        {children({ width, height })}
      </div>
    </div>
  );
}
