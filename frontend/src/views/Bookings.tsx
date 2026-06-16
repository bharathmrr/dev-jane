import { useEffect, useState } from 'react';
import { api } from '../api';
import { Panel } from '../ui';

export default function Bookings() {
  const [leads, setLeads] = useState<any[]>([]);
  useEffect(() => { api('/leads').then(setLeads).catch(() => {}); }, []);
  const booked = leads.filter((l) => l.booked_at && !l.dropped);
  return (
    <Panel className="overflow-x-auto">
      <h3 className="font-bold mb-3 text-slate-800">Booked Meetings — {booked.length}</h3>
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-slate-500 border-b"><th className="py-2">Company</th><th>When</th><th>Meeting</th></tr>
        </thead>
        <tbody>
          {booked.map((l) => (
            <tr key={l.lead_id} className="border-b last:border-0">
              <td className="py-2"><div className="font-semibold">{l.company}</div><div className="text-xs text-slate-500">{l.email}</div></td>
              <td className="text-sm">{l.selected_slot || l.booked_at}</td>
              <td>{l.meeting_link ? <a className="text-blue-600 text-xs underline" target="_blank" href={l.meeting_link}>Join ↗</a> : '—'}</td>
            </tr>
          ))}
          {booked.length === 0 && <tr><td colSpan={3} className="text-center text-slate-400 py-6">No bookings yet</td></tr>}
        </tbody>
      </table>
    </Panel>
  );
}
