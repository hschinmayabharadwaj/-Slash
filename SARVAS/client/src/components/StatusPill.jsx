import { cn } from '@/lib/utils';
import { TONE_CLASS, taskStatus } from '@/lib/status';

export default function StatusPill({ status, className }) {
  const { label, tone } = taskStatus(status);
  return (
    <span className={cn('tag whitespace-nowrap', TONE_CLASS[tone], className)}>
      <span className={cn('size-1.5 rounded-full bg-current', tone === 'working' && 'motion-safe:animate-pulse')} />
      {label}
    </span>
  );
}
