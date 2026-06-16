import { useEffect, useState } from 'react';
import { api } from '../api';

const pct = (a: number, b: number) => (b > 0 ? Math.round((a / b) * 100) : 0);

export default function Overview() {
  const [ov, setOv] = useState<any>(null);
  const [err, setErr] = useState('');
  useEffect(() => {
    api('/overview').then(setOv).catch((e) => setErr(e.message));
  }, []);
  if (err) return <div className="text-red-600">{err}</div>;
  if (!ov) return <div className="text-slate-400">Loading…</div>;
  const t = ov.totals;

  const tiles = [
    { label: 'Total Leads', value: t.leads, sub: '', grad: 'from-indigo-500 to-indigo-700' },
    { label: 'Engaged', value: t.replied, sub: pct(t.replied, t.leads) + '% reply rate', grad: 'from-emerald-500 to-emerald-600' },
    { label: 'Active Deals', value: t.onboarding, sub: pct(t.onboarding, t.leads) + '% of leads', grad: 'from-blue-500 to-blue-600' },
    { label: 'NDAs Signed', value: t.nda_signed, sub: '', grad: 'from-violet-500 to-violet-700' },
    { label: 'Deals Won', value: t.agreement_signed, sub: pct(t.agreement_signed, t.leads) + '% win rate', grad: 'from-teal-500 to-emerald-600' },
    { label: 'Action Needed', value: t.pending_approvals, sub: 'approvals to review', grad: 'from-rose-500 to-rose-600' },
  ];
  const cf: [string, number][] = [
    ['Leads', t.leads], ['Engaged', t.replied], ['Active Deals', t.onboarding],
    ['NDAs Signed', t.nda_signed], ['Deals Won', t.agreement_signed],
  ];
  const cmax = Math.max(t.leads, 1);

  return (
    <div>
      <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-4 mb-6">
        {tiles.map((c) => (
          <div key={c.label} className={`rounded-2xl p-4 text-white bg-gradient-to-br ${c.grad} shadow-lg hover:-translate-y-0.5 transition-transform`}>
            <div className="text-[10.5px] font-bold uppercase tracking-wider opacity-90">{c.label}</div>
            <div className="text-3xl font-extrabold mt-2 leading-none">{c.value}</div>
            {c.sub && <div className="text-xs opacity-90 mt-1.5">{c.sub}</div>}
          </div>
        ))}
      </div>
      <div className="bg-white rounded-2xl shadow p-5 max-w-2xl">
        <h3 className="font-bold mb-4 text-slate-800">Conversion Funnel</h3>
        {cf.map(([label, v]) => {
          const of = Math.round((v / cmax) * 100);
          return (
            <div key={label} className="flex items-center gap-3 mb-2.5">
              <div className="w-28 text-sm text-slate-600">{label}</div>
              <div className="flex-1 bg-slate-100 rounded-full h-6 overflow-hidden">
                <div
                  className="h-full bg-gradient-to-r from-blue-500 to-indigo-600 rounded-full flex items-center justify-end pr-2 text-white text-xs font-semibold"
                  style={{ width: Math.max(of, v ? 12 : 0) + '%' }}
                >
                  {v} · {of}%
                </div>
              </div>
            </div>
          );
        })}
      </div>
      <p className="text-xs text-slate-400 mt-4">Updated {ov.generated_at}</p>
    </div>
  );
}
