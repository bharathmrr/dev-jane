import type { ReactNode } from 'react';

const TONE: Record<string, string> = {
  green: 'bg-emerald-100 text-emerald-700',
  amber: 'bg-amber-100 text-amber-700',
  blue: 'bg-blue-100 text-blue-700',
  red: 'bg-rose-100 text-rose-700',
  gray: 'bg-slate-100 text-slate-600',
  purple: 'bg-violet-100 text-violet-700',
};

// One status -> tone map shared across views (mirrors the legacy chip() colours).
export function chipTone(s: string): string {
  const m: Record<string, string> = {
    APPROVED: 'green', SIGNED: 'green', PROCEED_NEXT: 'green', BOOKED: 'green', REPLIED: 'green', COMPLETED: 'green',
    PENDING: 'gray', NEW: 'gray',
    FORM_SENT: 'amber', SUBMITTED: 'amber', UNDER_REVIEW: 'amber', TEAM_REVIEW: 'amber', DRAFT_GENERATED: 'amber',
    SIGNED_RECEIVED: 'amber', SIGN_UNDER_REVIEW: 'amber', JOB_CHANGED: 'amber',
    SENT: 'blue', SENT_TO_LEAD: 'blue',
    FAILED: 'red', REJECTED: 'red', DROPPED: 'red', INVALID_EMAIL: 'red', SIGN_REJECTED: 'red', DRAFT_REJECTED: 'red',
  };
  return m[s] || 'gray';
}

export function Chip({ s, tone }: { s: string; tone?: string }) {
  const t = tone || chipTone(s);
  return (
    <span className={`inline-block px-2.5 py-0.5 rounded-full text-[10.5px] font-bold tracking-wide ${TONE[t] || TONE.gray}`}>
      {s || '—'}
    </span>
  );
}

export function Panel({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <div className={`bg-white rounded-2xl shadow p-5 ${className}`}>{children}</div>;
}
