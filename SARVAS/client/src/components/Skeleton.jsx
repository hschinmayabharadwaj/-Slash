import { cn } from '@/lib/utils';

export function Skeleton({ className }) {
  return <div className={cn('rounded-md bg-muted motion-safe:animate-pulse', className)} />;
}

export function PageLoading() {
  return (
    <div className="space-y-4" aria-busy="true" aria-label="Loading">
      <Skeleton className="h-28" />
      <Skeleton className="h-48" />
      <Skeleton className="h-24" />
    </div>
  );
}
