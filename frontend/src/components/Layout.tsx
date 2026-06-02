import { useState } from 'react'
import { Outlet, NavLink, useNavigate } from 'react-router-dom'
import { clearSession, getUserInfo } from '../api/client'

const DashboardIcon = (
  <svg className="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M4 6a2 2 0 012-2h2a2 2 0 012 2v4a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v4a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z"></path>
  </svg>
)

const OrganizersIcon = (
  <svg className="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197M13 7a4 4 0 11-8 0 4 4 0 018 0z"></path>
  </svg>
)

const BookingsIcon = (
  <svg className="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"></path>
  </svg>
)

const EmailIcon = (
  <svg className="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"></path>
  </svg>
)

const adminNav = [
  { to: '/', label: 'Dashboard', icon: DashboardIcon },
  { to: '/organizers', label: 'Organizer Mgmt', icon: OrganizersIcon },
  { to: '/bookings', label: 'All Bookings', icon: BookingsIcon },
  { to: '/leads/new', label: 'Send Email', icon: EmailIcon },
]

const organizerNav = [
  { to: '/', label: 'Dashboard', icon: DashboardIcon },
  { to: '/bookings', label: 'My Bookings', icon: BookingsIcon },
  { to: '/leads/new', label: 'Send Email', icon: EmailIcon },
]

const viewerNav = [
  { to: '/', label: 'Dashboard', icon: DashboardIcon },
  { to: '/bookings', label: 'My Bookings', icon: BookingsIcon },
]

export default function Layout() {
  const navigate = useNavigate()
  const user = getUserInfo()
  const role = user?.role ?? 'agent'

  const [collapsed, setCollapsed] = useState(() => localStorage.getItem('sidebar_collapsed') === 'true')

  const nav = role === 'admin' ? adminNav : role === 'organizer' ? organizerNav : viewerNav

  const toggleSidebar = () => {
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
    <div className="flex h-screen bg-gray-50">
      {/* Sidebar */}
      <aside className={`bg-white border-r border-gray-200 flex flex-col transition-all duration-300 relative ${collapsed ? 'w-16' : 'w-56'}`}>
        {/* Toggle Collapse Button */}
        <button
          onClick={toggleSidebar}
          className="absolute -right-3 top-6 bg-white border border-gray-200 text-gray-500 hover:text-gray-700 w-6 h-6 rounded-full flex items-center justify-center shadow-sm cursor-pointer z-10 hover:shadow transition-all"
        >
          {collapsed ? (
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" d="M9 5l7 7-7 7"></path>
            </svg>
          ) : (
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" d="M15 19l-7-7 7-7"></path>
            </svg>
          )}
        </button>

        {/* Header */}
        <div className={`py-4 border-b border-gray-100 flex flex-col items-center justify-center ${collapsed ? 'px-2' : 'px-5'}`}>
          {collapsed ? (
            <div className="w-10 h-10 bg-gradient-to-tr from-blue-600 to-indigo-600 rounded-lg flex items-center justify-center text-white font-bold text-lg shadow-sm">
              MS
            </div>
          ) : (
            <div className="w-full">
              <p className="text-sm font-bold bg-gradient-to-r from-blue-600 to-indigo-600 bg-clip-text text-transparent">Meeting Scheduler</p>
              <div className="flex items-center gap-1.5 mt-1">
                <span className={`text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded font-bold ${
                  role === 'admin' ? 'bg-purple-50 text-purple-700 border border-purple-100' :
                  role === 'organizer' ? 'bg-blue-50 text-blue-700 border border-blue-100' :
                  'bg-gray-50 text-gray-500 border border-gray-100'
                }`}>
                  {role === 'admin' ? 'Admin' : role === 'organizer' ? 'Organizer' : 'Viewer'}
                </span>
              </div>
              {user && (
                <p className="text-xs text-gray-400 mt-1.5 truncate font-medium">{user.full_name}</p>
              )}
            </div>
          )}
        </div>

        {/* Nav Links */}
        <nav className={`flex-1 py-4 space-y-1 ${collapsed ? 'px-2' : 'px-3'}`}>
          {nav.map(({ to, label, icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              className={({ isActive }) =>
                `flex items-center ${collapsed ? 'justify-center py-2.5' : 'gap-3 px-3 py-2'} rounded-md text-sm font-medium transition-all ${
                  isActive
                    ? 'bg-blue-50 text-blue-700'
                    : 'text-gray-600 hover:bg-gray-50 hover:text-gray-900'
                }`
              }
              title={collapsed ? label : undefined}
            >
              <span className={`shrink-0 ${collapsed ? 'w-10 flex items-center justify-center' : ''}`}>
                {icon}
              </span>
              {!collapsed && <span className="truncate">{label}</span>}
            </NavLink>
          ))}
        </nav>

        {/* Footer / Logout */}
        <div className={`py-4 border-t border-gray-100 ${collapsed ? 'px-2' : 'px-3'}`}>
          <button
            onClick={logout}
            className={`w-full flex items-center ${collapsed ? 'justify-center py-2.5' : 'gap-3 px-3 py-2'} rounded-md text-sm font-medium text-gray-600 hover:bg-red-50 hover:text-red-600 transition-colors`}
            title={collapsed ? "Logout" : undefined}
          >
            <span className={`shrink-0 ${collapsed ? 'w-10 flex items-center justify-center' : ''}`}>
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"></path>
              </svg>
            </span>
            {!collapsed && <span>Logout</span>}
          </button>
        </div>
      </aside>

      {/* Main */}
      <main className="flex-1 overflow-y-auto">
        <div className="max-w-5xl mx-auto px-6 py-8">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
