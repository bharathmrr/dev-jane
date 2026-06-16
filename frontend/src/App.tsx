import { useState } from 'react';
import type { ReactNode } from 'react';
import { getAuth, login, logout, setAuth, canAct } from './api';
import { AppProvider, useApp } from './store';
import LeadDrawer from './views/LeadDrawer';
import AddLeadModal from './views/AddLeadModal';
import Overview from './views/Overview';
import Kanban from './views/Kanban';
import Leads from './views/Leads';
import Sheet from './views/Sheet';
import Bookings from './views/Bookings';
import Documents from './views/Documents';
import Approvals from './views/Approvals';
import Activity from './views/Activity';
import Users from './views/Users';

const VIEWS: Record<string, () => ReactNode> = {
  overview: () => <Overview />,
  kanban: () => <Kanban />,
  leads: () => <Leads />,
  sheet: () => <Sheet />,
  bookings: () => <Bookings />,
  docs: () => <Documents />,
  approvals: () => <Approvals />,
  logs: () => <Activity />,
  users: () => <Users />,
};

const NAVS: [string, string][] = [
  ['overview', 'Overview'], ['kanban', 'Pipeline Board'], ['leads', 'Leads'],
  ['sheet', 'Sheet'], ['bookings', 'Bookings'], ['docs', 'Documents'],
  ['approvals', 'Notifications'], ['logs', 'Activity'], ['users', 'Users'],
];

function Login({ onDone }: { onDone: () => void }) {
  const [email, setEmail] = useState('');
  const [pass, setPass] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);
  async function submit() {
    setErr(''); setBusy(true);
    try {
      const d = await login(email.trim(), pass);
      setAuth(d.access_token, d.role, d.full_name);
      onDone();
    } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  }
  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-b from-slate-100 to-slate-50 p-4">
      <div className="w-full max-w-sm bg-white rounded-2xl shadow-xl p-8">
        <div className="text-xl font-extrabold text-blue-800 mb-1">JANE AEROSPACE</div>
        <h1 className="text-lg font-bold mb-1">Command Center</h1>
        <p className="text-sm text-slate-500 mb-5">Outreach · Bookings · KYC · NDA · Agreements.</p>
        <label className="block text-xs font-semibold text-slate-600 mb-1">Email</label>
        <input className="w-full border border-slate-300 rounded-lg px-3 py-2 mb-3 text-sm outline-none focus:border-blue-500"
          value={email} onChange={e => setEmail(e.target.value)} placeholder="admin@janeaerospace.co.in" />
        <label className="block text-xs font-semibold text-slate-600 mb-1">Password</label>
        <input type="password" className="w-full border border-slate-300 rounded-lg px-3 py-2 mb-3 text-sm outline-none focus:border-blue-500"
          value={pass} onChange={e => setPass(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && submit()} placeholder="••••••••" />
        {err && <div className="text-sm text-red-600 mb-3">{err}</div>}
        <button disabled={busy} onClick={submit}
          className="w-full bg-blue-600 hover:bg-blue-700 text-white font-bold rounded-lg py-2.5 text-sm disabled:opacity-60">
          {busy ? 'Signing in…' : 'Sign In'}
        </button>
      </div>
    </div>
  );
}

function Shell() {
  const { name, role } = getAuth();
  const [view, setView] = useState('overview');
  const { query, setQuery, refreshKey, refresh, toast } = useApp();
  const [showAdd, setShowAdd] = useState(false);
  const navs = NAVS.filter(([v]) => !(v === 'users' && role !== 'admin'));
  return (
    <div className="flex min-h-screen">
      <aside className="w-56 shrink-0 bg-slate-900 text-slate-300 flex flex-col">
        <div className="px-5 py-4 text-white font-extrabold tracking-wide">JANE AEROSPACE</div>
        <nav className="flex-1 px-2">
          {navs.map(([v, l]) => (
            <button key={v} onClick={() => setView(v)}
              className={`w-full text-left px-3 py-2 rounded-lg text-sm mb-0.5 transition-colors ${view === v ? 'bg-slate-700 text-white' : 'hover:bg-slate-800'}`}>
              {l}
            </button>
          ))}
        </nav>
        <div className="px-4 py-3 border-t border-slate-700 flex items-center gap-2 text-sm">
          <div className="w-8 h-8 rounded-full bg-blue-600 text-white flex items-center justify-center font-bold">{(name || 'A')[0]}</div>
          <div className="flex-1 min-w-0">
            <div className="font-semibold truncate text-white">{name || '—'}</div>
            <div className="text-xs text-slate-400">{role}</div>
          </div>
          <button onClick={logout} className="text-slate-400 hover:text-white" title="Sign out">⎋</button>
        </div>
      </aside>
      <div className="flex-1 flex flex-col min-w-0">
        <header className="flex items-center gap-3 px-8 py-3.5 bg-white border-b border-slate-200">
          <h2 className="text-xl font-bold">{navs.find(([v]) => v === view)?.[1]}</h2>
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search leads, companies…"
            className="ml-auto w-64 border border-slate-300 rounded-lg px-3 py-1.5 text-sm outline-none focus:border-blue-500" />
          {canAct() && (
            <button onClick={() => setShowAdd(true)} className="bg-blue-600 hover:bg-blue-700 text-white rounded-lg px-3 py-1.5 text-sm font-semibold">+ Add Lead</button>
          )}
          <button onClick={refresh} className="border border-slate-300 rounded-lg px-3 py-1.5 text-sm" title="Refresh">↻</button>
        </header>
        <main className="flex-1 p-8 overflow-auto">
          <div key={view + '-' + refreshKey}>
            {(VIEWS[view] || (() => <p className="text-slate-500">Coming soon.</p>))()}
          </div>
        </main>
      </div>
      <LeadDrawer />
      {showAdd && <AddLeadModal onClose={() => setShowAdd(false)} />}
      {toast && (
        <div className={`fixed bottom-5 right-5 z-50 px-4 py-2.5 rounded-lg text-white text-sm font-semibold shadow-lg ${toast.ok ? 'bg-emerald-600' : 'bg-rose-600'}`}>{toast.msg}</div>
      )}
    </div>
  );
}

export default function App() {
  const [authed, setAuthed] = useState(!!getAuth().token);
  if (!authed) return <Login onDone={() => setAuthed(true)} />;
  return (
    <AppProvider>
      <Shell />
    </AppProvider>
  );
}
