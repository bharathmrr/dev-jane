import { useEffect, useState } from 'react';
import { api } from '../api';
import { Chip, Panel } from '../ui';

function DocCell({ o, k }: { o: any; k: string }) {
  return (
    <div className="space-y-1">
      <div className="flex gap-1 items-center flex-wrap">
        <Chip s={o[k + '_status'] || 'PENDING'} />
        {o[k + '_signed'] ? <Chip s="✓ SIGNED" tone="green" /> : null}
      </div>
      <div className="flex gap-2">
        <a className="text-blue-600 text-xs font-semibold underline" target="_blank" href={o[k + '_edit_url']}>Edit &amp; Preview</a>
        <a className="text-slate-500 text-xs" target="_blank" href={o[k + '_sign_url']}>Sign Link</a>
      </div>
    </div>
  );
}

export default function Documents() {
  const [leads, setLeads] = useState<any[]>([]);
  useEffect(() => { api('/leads').then(setLeads).catch(() => {}); }, []);
  const rows = leads.filter((l) => l.onboarding);
  return (
    <Panel className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-slate-500 border-b">
            <th className="py-2">Company</th><th>KYC</th><th>NDA</th><th>Agreement</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((l) => {
            const o = l.onboarding;
            return (
              <tr key={l.lead_id} className="border-b last:border-0 align-top">
                <td className="py-3">
                  <div className="font-bold">{l.company}</div>
                  <div className="text-xs text-slate-500 mb-1">{l.email}</div>
                  <Chip s={o.company_type === 'overseas' ? 'Overseas' : 'Indian'} tone={o.company_type === 'overseas' ? 'amber' : 'blue'} />
                </td>
                <td className="py-3">
                  <Chip s={o.kyc_status || 'PENDING'} />
                  <div><a className="text-blue-600 text-xs underline" target="_blank" href={o.kyc_view_url}>View</a></div>
                </td>
                <td className="py-3"><DocCell o={o} k="nda" /></td>
                <td className="py-3"><DocCell o={o} k="agreement" /></td>
              </tr>
            );
          })}
          {rows.length === 0 && <tr><td colSpan={4} className="text-center text-slate-400 py-6">No onboarding records yet</td></tr>}
        </tbody>
      </table>
    </Panel>
  );
}
