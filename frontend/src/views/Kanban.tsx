import { useEffect, useState } from 'react';
import { api } from '../api';
import { useApp } from '../store';
import { Chip } from '../ui';

const COLS = [
  { label: 'New / Outreach', idx: [0, 1, 2], bg: 'bg-slate-200 text-slate-700' },
  { label: 'Meeting Booked', idx: [3], bg: 'bg-emerald-200 text-emerald-800' },
  { label: 'KYC', idx: [4, 5], bg: 'bg-amber-200 text-amber-800' },
  { label: 'NDA', idx: [6, 7], bg: 'bg-violet-200 text-violet-800' },
  { label: 'Agreement', idx: [8], bg: 'bg-blue-200 text-blue-800' },
  { label: 'Completed', idx: [9], bg: 'bg-emerald-300 text-emerald-900' },
];

function cardChip(l: any, label: string) {
  const ob = l.onboarding || {};
  const when = l.selected_slot || l.booked_at;
  if (label === 'New / Outreach' || label === 'Meeting Booked')
    return when ? <Chip s={when} tone="green" /> : <Chip s="not booked" tone="gray" />;
  if (label === 'KYC') return <Chip s={ob.kyc_status ? 'KYC ' + ob.kyc_status : 'KYC pending'} tone={ob.kyc_status ? 'blue' : 'gray'} />;
  if (label === 'NDA') return ob.nda_signed ? <Chip s="NDA ✓" tone="green" /> : <Chip s={ob.nda_status ? 'NDA ' + ob.nda_status : 'NDA pending'} tone={ob.nda_status ? 'amber' : 'gray'} />;
  if (label === 'Agreement') return ob.agreement_signed ? <Chip s="Agreement ✓" tone="green" /> : <Chip s={ob.agreement_status || 'Agreement pending'} tone={ob.agreement_status ? 'amber' : 'gray'} />;
  return <Chip s="Completed" tone="green" />;
}

export default function Kanban() {
  const [leads, setLeads] = useState<any[]>([]);
  const { openLead, query } = useApp();
  useEffect(() => { api('/leads').then(setLeads).catch(() => {}); }, []);
  const ql = query.toLowerCase();
  return (
    <div className="flex gap-4 overflow-x-auto pb-4">
      {COLS.map((col) => {
        const cards = leads.filter((l) => col.idx.includes(l.stage_index) && !l.dropped
          && (!ql || (l.company || '').toLowerCase().includes(ql) || l.email.toLowerCase().includes(ql)));
        return (
          <div key={col.label} className="w-64 shrink-0">
            <div className={`flex items-center justify-between px-3 py-2 rounded-xl font-semibold text-sm mb-2 ${col.bg}`}>
              <span>{col.label}</span>
              <span className="bg-white/70 rounded-full px-2 text-xs">{cards.length}</span>
            </div>
            <div className="space-y-2">
              {cards.map((l) => (
                <div key={l.lead_id} onClick={() => openLead(l)} className="bg-white rounded-xl shadow-sm p-3 hover:shadow-md transition cursor-pointer">
                  <div className="font-bold text-sm text-slate-800">{l.company}</div>
                  {l.contact && <div className="text-xs text-slate-500">{l.contact}</div>}
                  <div className="text-xs text-slate-500 mb-2">{l.email}</div>
                  <div className="flex flex-wrap gap-1">
                    {cardChip(l, col.label)}
                    {l.followups ? <Chip s={'+' + l.followups + ' fup'} tone="gray" /> : null}
                  </div>
                </div>
              ))}
              {cards.length === 0 && <div className="text-center text-xs text-slate-400 py-3">No leads</div>}
            </div>
          </div>
        );
      })}
    </div>
  );
}
