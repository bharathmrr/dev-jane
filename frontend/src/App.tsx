import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import Layout from './components/Layout'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import Leads from './pages/Leads'
import Bookings from './pages/Bookings'
import CreateLead from './pages/CreateLead'
import OrganizerManagement from './pages/OrganizerManagement'
import { getUserInfo } from './api/client'

function isAuthenticated() {
  return !!localStorage.getItem('access_token')
}

function PrivateRoute({ children }: { children: React.ReactNode }) {
  return isAuthenticated() ? <>{children}</> : <Navigate to="/login" replace />
}

function AdminRoute({ children }: { children: React.ReactNode }) {
  const user = getUserInfo()
  if (!isAuthenticated()) return <Navigate to="/login" replace />
  if (user?.role !== 'admin') return <Navigate to="/" replace />
  return <>{children}</>
}

function ViewerBlock({ children }: { children: React.ReactNode }) {
  // Viewers (agent role) cannot send emails or take write actions
  const user = getUserInfo()
  if (user?.role === 'agent') return <Navigate to="/" replace />
  return <>{children}</>
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route
          path="/"
          element={
            <PrivateRoute>
              <Layout />
            </PrivateRoute>
          }
        >
          <Route index element={<Dashboard />} />

          {/* All authenticated users */}
          <Route path="bookings" element={<PrivateRoute><Bookings /></PrivateRoute>} />

          {/* Admin + Organizer only (agents blocked) */}
          <Route path="send-email" element={<ViewerBlock><CreateLead /></ViewerBlock>} />

          {/* Admin only */}
          <Route path="leads" element={<AdminRoute><Leads /></AdminRoute>} />
          <Route path="organizers" element={<AdminRoute><OrganizerManagement /></AdminRoute>} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
