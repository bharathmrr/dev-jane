import { useState } from 'react'
import { Outlet, NavLink, useNavigate } from 'react-router-dom'
import {
  LayoutDashboard, Users, CalendarDays, LogOut, Mail,
  ChevronLeft, ChevronRight, Plane,
} from 'lucide-react'
import { clearSession, getUserInfo } from '../api/client'

const adminNav = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard },
  { to: '/leads', label: 'Leads', icon: Mail },
  { to: '/organizers', label: 'Organizer Mgmt', icon: Users },
]

const organizerNav = [
  { to: '/', label: 'My Bookings', icon: CalendarDays },
  { to: '/leads', label: 'My Leads', icon: Mail },
]

const viewerNav = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard },
]

const roleLabel: Record<string, string> = {
  admin: 'Super Admin',
  organizer: 'Organizer',
  agent: 'Viewer',
}

const roleColor: Record<string, string> = {
  admin: 'bg-violet-100 text-violet-700',
  organizer: 'bg-blue-100 text-blue-700',
  agent: 'bg-gray-100 text-gray-500',
}

export default function Layout() {
  const navigate = useNavigate()
  const user = getUserInfo()
  const role = user?.role ?? 'agent'
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem('sidebar_collapsed') === 'true')

  const nav = role === 'admin' ? adminNav : role === 'organizer' ? organizerNav : viewerNav

  function toggle() {
    setCollapsed(prev => {
      const next = !prev
      localStorage.setItem('sidebar_collapsed', String(next))
      return next
    })
  }

  function logout() {
    clearSession()
    navigate('/login')
  }

  return (
    <div className="flex h-screen bg-slate-50">
      {/* Sidebar */}
      <aside className={`bg-white border-r border-gray-100 flex flex-col relative transition-all duration-200 ${collapsed ? 'w-14' : 'w-52'}`}>

        {/* Collapse toggle */}
        <button
          onClick={toggle}
          className="absolute -right-3 top-5 z-10 w-6 h-6 rounded-full bg-white border border-gray-200 shadow-sm flex items-center justify-center text-gray-400 hover:text-gray-700 transition-colors"
          title={collapsed ? 'Expand' : 'Collapse'}
        >
          {collapsed ? <ChevronRight className="w-3 h-3" /> : <ChevronLeft className="w-3 h-3" />}
        </button>

        {/* Brand */}
        <div className={`flex items-center gap-2.5 border-b border-gray-100 ${collapsed ? 'justify-center px-0 py-3.5' : 'px-4 py-3.5'}`}>
          <div className="w-7 h-7 rounded-md bg-blue-600 flex items-center justify-center shrink-0">
            <Plane className="w-4 h-4 text-white" />
          </div>
          {!collapsed && (
            <div className="min-w-0">
              <p className="text-xs font-bold text-gray-900 leading-tight truncate">Jane Aerospace</p>
              <p className="text-[10px] text-gray-400 leading-tight">Meeting Scheduler</p>
            </div>
          )}
        </div>

        {/* Nav */}
        <nav className={`flex-1 py-2 space-y-0.5 ${collapsed ? 'px-1.5' : 'px-2'}`}>
          {nav.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              title={collapsed ? label : undefined}
              className={({ isActive }) =>
                `flex items-center gap-2.5 rounded-md text-sm font-medium transition-colors ${collapsed ? 'justify-center px-0 py-2' : 'px-2.5 py-2'} ${
                  isActive
                    ? 'bg-blue-50 text-blue-700'
                    : 'text-gray-500 hover:bg-gray-50 hover:text-gray-800'
                }`
              }
            >
              <Icon className="w-4 h-4 shrink-0" />
              {!collapsed && <span className="truncate">{label}</span>}
            </NavLink>
          ))}
        </nav>

        {/* User + Logout */}
        <div className={`border-t border-gray-100 py-3 ${collapsed ? 'px-1.5' : 'px-2'}`}>
          {!collapsed && user && (
            <div className="px-2.5 pb-2.5">
              <p className="text-xs font-semibold text-gray-800 truncate">{user.full_name}</p>
              <p className="text-[10px] text-gray-400 truncate">{user.email}</p>
              <span className={`inline-block mt-1 text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ${roleColor[role]}`}>
                {roleLabel[role]}
              </span>
            </div>
          )}
          <button
            onClick={logout}
            title={collapsed ? 'Logout' : undefined}
            className={`w-full flex items-center gap-2.5 rounded-md text-sm font-medium text-gray-500 hover:bg-red-50 hover:text-red-600 transition-colors ${collapsed ? 'justify-center px-0 py-2' : 'px-2.5 py-2'}`}
          >
            <LogOut className="w-4 h-4 shrink-0" />
            {!collapsed && <span>Sign out</span>}
          </button>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-y-auto">
        <div className="max-w-5xl mx-auto px-8 py-6">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
