import { cn } from "@/lib/utils";

// A CSS loop keeps the sweep smooth while the main thread is busy, which is
// exactly when shimmer text tends to be on screen. The keyframes live in
// styles.css: the CSP blocks an inline <style>.

export default function ShimmerText({
  text = "Text Shimmer",
  className,
  children,
  as: Component = "span",
  ...props
}) {
  return (
    <Component
        {...props}
        className={cn(
          "bg-[length:200%_100%] bg-gradient-to-r from-neutral-950 via-neutral-400 to-neutral-950 bg-clip-text font-bold text-3xl text-transparent motion-safe:animate-[kokonutui-shimmer-text_2.5s_linear_infinite] dark:from-white dark:via-neutral-600 dark:to-white",
          className
        )}
      >
        {children ?? text}
      </Component>
  );
}
