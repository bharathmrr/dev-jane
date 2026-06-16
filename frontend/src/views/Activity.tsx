import { useEffect, useState } from 'react';
import { api } from '../api';
import { Panel } from '../ui';

function actColor(ev: string) {
  ev = (ev || '').toUpperCase();
  if (/SIGN|APPROV|COMPLETE|BOOK/.test(ev)) return 'text-emerald-600';
  if (/REJECT|FAIL|DROP|INVALID/.test(ev)) return 'text-rose-600';
  if (/SENT|ADD|UPDATE|CREATE/.test(ev)) return 'text-blue-600';
  if (/REPL|REVIEW|FOLLOW/.test(ev)) return 'text-amber-600';
  return 'text-slate-500';
}

export default function Activity() {
  const [rows, setRows] = useState<any[]>([]);
  useEffect(() => { api('/logs?limit=150').then(setRows).catch(() => {}); }, []);
  return (
    <Panel className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-slate-500 border-b"><th className="py-2">Time</th><th>Event</th><th>Company</th><th>Detail</th></tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-b last:border-0">
              <td className="py-2 text-xs text-slate-400 whitespace-nowrap">{r.ts}</td>
              <td className={`text-xs font-bold ${actColor(r.event)}`}>{(r.event || '').replace(/_/g, ' ')}</td>
              <td className="text-xs">{r.company}</td>
              <td className="text-xs text-slate-600">{r.detail}</td>
            </tr>
          ))}
          {rows.length === 0 && <tr><td colSpan={4} className="text-center text-slate-400 py-6">No events yet</td></tr>}
        </tbody>
      </table>
    </Panel>
  );
}
