import { cn } from "@/lib/utils";

// Solid ink with a CSS pulse (no gradient text); a CSS loop stays smooth
// while the main thread is busy, which is when this text is on screen.

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
          "font-bold text-3xl text-muted-foreground motion-safe:animate-pulse",
          className
        )}
      >
        {children ?? text}
      </Component>
  );
}
