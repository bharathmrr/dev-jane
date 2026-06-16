import { useEffect, useState } from 'react';
import { api } from '../api';
import { Chip, Panel } from '../ui';

export default function Users() {
  const [rows, setRows] = useState<any[]>([]);
  useEffect(() => { api('/users').then(setRows).catch(() => {}); }, []);
  return (
    <Panel className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-slate-500 border-b"><th className="py-2">Name</th><th>Email</th><th>Role</th><th>Status</th></tr>
        </thead>
        <tbody>
          {rows.map((u: any) => (
            <tr key={u.id} className="border-b last:border-0">
              <td className="py-2 font-semibold">{u.full_name}</td>
              <td className="text-slate-600">{u.email}</td>
              <td><Chip s={u.role} tone={u.role === 'admin' ? 'purple' : u.role === 'approver' ? 'blue' : 'gray'} /></td>
              <td>{u.is_active ? <Chip s="active" tone="green" /> : <Chip s="disabled" tone="red" />}</td>
            </tr>
          ))}
          {rows.length === 0 && <tr><td colSpan={4} className="text-center text-slate-400 py-6">No users</td></tr>}
        </tbody>
      </table>
    </Panel>
  );
}
