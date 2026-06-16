import { useState } from 'react';
import { api, canAct } from '../api';
import { useApp } from '../store';
import { Chip } from '../ui';

export default function LeadDrawer() {
  const { lead, closeLead, refresh, showToast } = useApp();
  const [busy, setBusy] = useState(false);
  if (!lead) return null;
  const ob = lead.onboarding;

  async function action(a: string, confirmMsg?: string) {
    if (confirmMsg && !confirm(confirmMsg)) return;
    setBusy(true);
    try {
      const d: any = await api(`/lead/${lead.lead_id}/action`, { method: 'POST', body: JSON.stringify({ action: a }) });
      showToast(d.message || 'Done'); refresh(); closeLead();
    } catch (e: any) { showToast(e.message, false); } finally { setBusy(false); }
  }

  return (
    <div className="fixed inset-0 z-40 flex justify-end" onClick={closeLead}>
      <div className="absolute inset-0 bg-black/30" />
      <div className="relative w-96 max-w-full bg-white h-full shadow-2xl p-5 overflow-auto" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-start justify-between">
          <div>
            <h3 className="text-lg font-bold">{lead.company}</h3>
            <div className="text-sm text-slate-500">{lead.email}</div>
          </div>
          <button onClick={closeLead} className="text-slate-400 hover:text-slate-700 text-lg">✕</button>
        </div>
        <div className="flex gap-2 mt-3 flex-wrap">
          <Chip s={lead.stage} tone="blue" />{lead.dropped && <Chip s="DROPPED" tone="red" />}
        </div>
        <div className="mt-4 space-y-1 text-sm text-slate-600">
          {lead.contact && <div><b>Contact:</b> {lead.contact}</div>}
          {lead.phone && <div><b>Phone:</b> {lead.phone}</div>}
          <div><b>Sent:</b> {lead.sent_at || '—'}</div>
          <div><b>Replied:</b> {lead.replied_at || '—'}</div>
          <div><b>Booked:</b> {lead.selected_slot || lead.booked_at || '—'}</div>
          {lead.meeting_link && <a href={lead.meeting_link} target="_blank" className="text-blue-600 font-semibold">🔗 Join Meeting ↗</a>}
        </div>
        {ob && (
          <div className="mt-4 p-3 bg-slate-50 rounded-xl text-sm space-y-1.5">
            <div className="flex items-center gap-2">KYC: <Chip s={ob.kyc_status || 'PENDING'} /></div>
            <div className="flex items-center gap-2">NDA: <Chip s={ob.nda_status || 'PENDING'} />{ob.nda_signed ? <Chip s="✓" tone="green" /> : null}</div>
            <div className="flex items-center gap-2">Agreement: <Chip s={ob.agreement_status || 'PENDING'} />{ob.agreement_signed ? <Chip s="✓" tone="green" /> : null}</div>
          </div>
        )}
        {canAct() && (
          <div className="mt-5 space-y-2">
            {!ob && (
              <button disabled={busy} onClick={() => action('start_onboarding', 'Start onboarding for ' + lead.company + '?')}
                className="w-full bg-emerald-600 hover:bg-emerald-700 text-white rounded-lg py-2 text-sm font-semibold disabled:opacity-60">▶ Start Onboarding</button>
            )}
            {ob && (
              <>
                <a href={ob.nda_edit_url} target="_blank" className="block text-center w-full bg-blue-600 hover:bg-blue-700 text-white rounded-lg py-2 text-sm font-semibold">Open NDA Editor</a>
                <a href={ob.agreement_edit_url} target="_blank" className="block text-center w-full bg-blue-600 hover:bg-blue-700 text-white rounded-lg py-2 text-sm font-semibold">Open Agreement Editor</a>
                <button disabled={busy} onClick={() => action('cancel_onboarding', 'Cancel onboarding for ' + lead.company + '?')}
                  className="w-full bg-amber-100 text-amber-800 rounded-lg py-2 text-sm font-semibold">Cancel Onboarding</button>
              </>
            )}
            {!lead.dropped
              ? <button disabled={busy} onClick={() => action('drop', 'Drop this lead?')} className="w-full bg-rose-100 text-rose-700 rounded-lg py-2 text-sm font-semibold">Drop Lead</button>
              : <button disabled={busy} onClick={() => action('restore')} className="w-full bg-slate-100 rounded-lg py-2 text-sm font-semibold">Restore Lead</button>}
          </div>
        )}
      </div>
    </div>
  );
}
