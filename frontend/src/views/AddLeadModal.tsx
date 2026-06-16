import { useState } from 'react';
import { api } from '../api';
import { useApp } from '../store';

const FIELDS: [string, string][] = [
  ['email', 'Email *'], ['company_name', 'Company'], ['contact_name', 'Contact name'], ['phone', 'Phone'],
];

export default function AddLeadModal({ onClose }: { onClose: () => void }) {
  const { refresh, showToast } = useApp();
  const [f, setF] = useState<any>({ email: '', company_name: '', contact_name: '', phone: '', start_onboarding: false });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  async function submit() {
    setErr(''); setBusy(true);
    try {
      const d: any = await api('/add-lead', { method: 'POST', body: JSON.stringify(f) });
      showToast(d.message || 'Lead added'); refresh(); onClose();
    } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  }

  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white rounded-2xl p-6 w-full max-w-md" onClick={(e) => e.stopPropagation()}>
        <h3 className="text-lg font-bold mb-4">Add Lead</h3>
        {FIELDS.map(([k, label]) => (
          <input key={k} className="w-full border border-slate-300 rounded-lg px-3 py-2 mb-3 text-sm outline-none focus:border-blue-500"
            placeholder={label} value={f[k]} onChange={(e) => setF({ ...f, [k]: e.target.value })} />
        ))}
        <label className="flex items-center gap-2 text-sm mb-4">
          <input type="checkbox" checked={f.start_onboarding} onChange={(e) => setF({ ...f, start_onboarding: e.target.checked })} /> Start onboarding now
        </label>
        {err && <div className="text-rose-600 text-sm mb-3">{err}</div>}
        <div className="flex gap-2 justify-end">
          <button onClick={onClose} className="px-4 py-2 rounded-lg bg-slate-100 text-sm">Cancel</button>
          <button disabled={busy} onClick={submit} className="px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-700 text-white text-sm font-bold disabled:opacity-60">
            {busy ? 'Adding…' : 'Add Lead'}
          </button>
        </div>
      </div>
    </div>
  );
}
