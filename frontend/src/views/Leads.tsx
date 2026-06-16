import { useEffect, useState } from 'react';
import { api } from '../api';
import { useApp } from '../store';
import { Chip, Panel } from '../ui';

export default function Leads() {
  const [leads, setLeads] = useState<any[]>([]);
  const { openLead, query } = useApp();
  useEffect(() => { api('/leads').then(setLeads).catch(() => {}); }, []);
  const ql = query.toLowerCase();
  const rows = leads.filter((l) => !query || l.email.toLowerCase().includes(ql) || (l.company || '').toLowerCase().includes(ql));
  return (
    <Panel className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-slate-500 border-b">
            <th className="py-2">Lead</th><th>Stage</th><th>Sent</th><th>Replied</th><th>Booked</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((l) => (
            <tr key={l.lead_id} onClick={() => openLead(l)} className={`border-b last:border-0 cursor-pointer hover:bg-slate-50 ${l.dropped ? 'opacity-50' : ''}`}>
              <td className="py-2">
                <div className="font-bold">{l.company}{l.dropped && <span className="ml-2"><Chip s="DROPPED" tone="red" /></span>}</div>
                <div className="text-xs text-slate-500">{l.email}{l.phone ? ' · ' + l.phone : ''}</div>
              </td>
              <td><Chip s={l.stage} tone="blue" /></td>
              <td className="text-xs">{l.sent_at || '—'}{l.followups ? <div className="text-[10px] text-slate-400">+{l.followups} follow-ups</div> : null}</td>
              <td className="text-xs">{l.replied_at || '—'}</td>
              <td className="text-xs">
                {l.booked_at || '—'}
                {l.selected_slot && <div className="text-[10px] text-slate-400">{l.selected_slot}</div>}
                {l.meeting_link && <a className="text-blue-600 text-[11px] font-semibold block" target="_blank" href={l.meeting_link}>🔗 Join ↗</a>}
              </td>
            </tr>
          ))}
          {rows.length === 0 && <tr><td colSpan={5} className="text-center text-slate-400 py-6">No leads found</td></tr>}
        </tbody>
      </table>
    </Panel>
  );
}
