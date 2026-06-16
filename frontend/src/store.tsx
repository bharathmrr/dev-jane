import { createContext, useContext, useState, type ReactNode } from 'react';

type Toast = { msg: string; ok: boolean } | null;
interface Ctx {
  lead: any | null;
  openLead: (l: any) => void;
  closeLead: () => void;
  refreshKey: number;
  refresh: () => void;
  query: string;
  setQuery: (q: string) => void;
  toast: Toast;
  showToast: (msg: string, ok?: boolean) => void;
}
const C = createContext<Ctx>(null as any);
export const useApp = () => useContext(C);

export function AppProvider({ children }: { children: ReactNode }) {
  const [lead, setLead] = useState<any>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [query, setQuery] = useState('');
  const [toast, setToast] = useState<Toast>(null);
  const showToast = (msg: string, ok = true) => {
    setToast({ msg, ok });
    setTimeout(() => setToast(null), 3000);
  };
  return (
    <C.Provider value={{
      lead, openLead: setLead, closeLead: () => setLead(null),
      refreshKey, refresh: () => setRefreshKey((k) => k + 1),
      query, setQuery, toast, showToast,
    }}>
      {children}
    </C.Provider>
  );
}
