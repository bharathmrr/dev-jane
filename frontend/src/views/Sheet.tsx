import { useEffect, useState } from 'react';
import { api } from '../api';
import { Panel } from '../ui';

export default function Sheet() {
  const [sheet, setSheet] = useState<any>(null);
  const [err, setErr] = useState('');
  useEffect(() => { api('/sheet').then(setSheet).catch((e) => setErr(e.message)); }, []);
  if (err) return <Panel><div className="text-rose-600">{err}</div></Panel>;
  if (!sheet) return <Panel><div className="text-slate-400">Loading sheet…</div></Panel>;
  return (
    <Panel className="overflow-x-auto">
      <div className="flex items-center gap-3 mb-3 flex-wrap">
        <h3 className="font-bold text-slate-800">{sheet.worksheet} — live Google Sheet</h3>
        <a className="ml-auto text-blue-600 text-sm font-semibold underline" target="_blank" href={sheet.sheet_url}>Open in Google Sheets ↗</a>
      </div>
      <table className="w-full text-xs border border-slate-200">
        <thead className="bg-slate-50">
          <tr>
            <th className="border border-slate-200 px-2 py-1 w-10"></th>
            {(sheet.headers || []).map((h: string, i: number) => (
              <th key={i} className="border border-slate-200 px-2 py-1 text-left font-semibold">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {(sheet.rows || []).map((r: string[], ri: number) => (
            <tr key={ri} className="hover:bg-slate-50">
              <td className="border border-slate-200 px-2 py-1 text-slate-400">{ri + 2}</td>
              {(sheet.headers || []).map((_: string, ci: number) => (
                <td key={ci} className="border border-slate-200 px-2 py-1">{r[ci] || ''}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="text-xs text-slate-400 mt-3">Inline editing is being ported next — for now edit in Google Sheets (it syncs to the DB &amp; CRM).</p>
    </Panel>
  );
}
