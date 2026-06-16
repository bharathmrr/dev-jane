import { useEffect, useState } from 'react';
import { api, canAct } from '../api';
import { useApp } from '../store';
import { Chip, Panel } from '../ui';

const APPROVABLE: Record<string, boolean> = { kyc: true, nda_draft: true, agreement_draft: true };

export default function Approvals() {
  const [d, setD] = useState<any>({ action: [], waiting: [] });
  const { refresh, refreshKey, showToast } = useApp();
  const [busy, setBusy] = useState('');
  useEffect(() => {
    api('/approvals').then((r: any) => setD(Array.isArray(r) ? { action: r, waiting: [] } : r)).catch(() => {});
  }, [refreshKey]);

  async function act(id: string, kind: string, action: string) {
    let notes = '';
    if (action === 'reject') { notes = prompt('Reason for rejection:') || ''; if (notes === '') return; }
    else if (!confirm(kind === 'kyc' ? 'Approve KYC? NDA will be generated.' : 'Approve and send to the lead?')) return;
    setBusy(id + kind);
    try {
      const r: any = await api('/approvals/act', { method: 'POST', body: JSON.stringify({ onboarding_id: id, kind, action, notes }) });
      showToast(r.message || 'Done'); refresh();
    } catch (e: any) { showToast(e.message, false); } finally { setBusy(''); }
  }

  return (
    <div className="space-y-4">
      <Panel className="overflow-x-auto">
        <h3 className="font-bold mb-3 text-slate-800">Needs Your Approval — {d.action.length}</h3>
        <table className="w-full text-sm">
          <thead><tr className="text-left text-slate-500 border-b"><th className="py-2">Type</th><th>Company</th><th>Since</th><th>Review</th><th>Decision</th></tr></thead>
          <tbody>
            {d.action.map((a: any) => (
              <tr key={a.onboarding_id + a.kind} className="border-b last:border-0">
                <td className="py-2"><Chip s={a.label} tone="amber" /></td>
                <td><div className="font-semibold">{a.company}</div><div className="text-xs text-slate-500">{a.email}</div></td>
                <td className="text-xs">{a.since || '—'}</td>
                <td>{a.view_url ? <a className="text-blue-600 text-xs underline" target="_blank" href={a.view_url}>Open</a> : '—'}</td>
                <td>
                  {canAct() && APPROVABLE[a.kind] ? (
                    <div className="flex gap-1">
                      <button disabled={!!busy} onClick={() => act(a.onboarding_id, a.kind, 'approve')} className="bg-emerald-600 text-white rounded px-2 py-1 text-xs font-semibold disabled:opacity-50">Approve</button>
                      <button disabled={!!busy} onClick={() => act(a.onboarding_id, a.kind, 'reject')} className="bg-rose-600 text-white rounded px-2 py-1 text-xs font-semibold disabled:opacity-50">Reject</button>
                    </div>
                  ) : <span className="text-xs text-slate-400">review via Open</span>}
                </td>
              </tr>
            ))}
            {d.action.length === 0 && <tr><td colSpan={5} className="text-center text-slate-400 py-5">Nothing pending — all caught up ✓</td></tr>}
          </tbody>
        </table>
      </Panel>
      <Panel className="overflow-x-auto">
        <h3 className="font-bold mb-3 text-slate-800">Waiting on Lead — {d.waiting.length}</h3>
        <table className="w-full text-sm">
          <thead><tr className="text-left text-slate-500 border-b"><th className="py-2">Type</th><th>Company</th><th>Since</th><th>Link</th></tr></thead>
          <tbody>
            {d.waiting.map((a: any) => (
              <tr key={a.onboarding_id + a.kind} className="border-b last:border-0">
                <td className="py-2"><Chip s={a.label} tone="gray" /></td>
                <td><div className="font-semibold">{a.company}</div><div className="text-xs text-slate-500">{a.email}</div></td>
                <td className="text-xs">{a.since || '—'}</td>
                <td>{a.view_url ? <a className="text-blue-600 text-xs underline" target="_blank" href={a.view_url}>Link</a> : '—'}</td>
              </tr>
            ))}
            {d.waiting.length === 0 && <tr><td colSpan={4} className="text-center text-slate-400 py-5">Nothing in flight</td></tr>}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}
