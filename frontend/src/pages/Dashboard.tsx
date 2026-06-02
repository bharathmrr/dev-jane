import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from 'recharts'
import { Calendar, List } from 'lucide-react'
import { getAnalytics, listBookings, getUserInfo } from '../api/client'
import BookingCalendar from '../components/BookingCalendar'
import type { AnalyticsSummary, BookingListItem } from '../api/types'
import { stateBadge } from '../lib/badges'

function StatCard({ label, value, accent }: { label: string; value: string | number; accent?: string }) {
  return (
    <div className="bg-white border border-gray-100 rounded-lg px-4 py-3">
      <p className="text-[11px] font-medium text-gray-400 uppercase tracking-wider">{label}</p>
      <p className={`text-2xl font-bold mt-0.5 ${accent ?? 'text-gray-900'}`}>{value}</p>
    </div>
  )
}

export default function Dashboard() {
  const user = getUserInfo()
  const isAdmin = user?.role === 'admin'
  const [viewMode, setViewMode] = useState<'table' | 'calendar'>('table')

  const { data: analytics, isLoading: loadingStats } = useQuery<AnalyticsSummary>({
    queryKey: ['analytics'],
    queryFn: () => getAnalytics().then((r) => r.data),
    enabled: isAdmin,
    refetchInterval: 30_000,
  })

  const { data: bookings, isLoading: loadingBookings } = useQuery<BookingListItem[]>({
    queryKey: ['bookings', 0, 100],
    queryFn: () => listBookings(0, 100).then((r) => r.data),
    refetchInterval: 30_000,
  })

  if (isAdmin) {
    // Admin dashboard — analytics
    const chartData = analytics ? [
      { name: 'Total', value: analytics.total_bookings, color: '#3b82f6' },
      { name: 'Confirmed', value: analytics.confirmed, color: '#22c55e' },
      { name: 'Cancelled', value: analytics.cancelled, color: '#ef4444' },
      { name: 'Pending', value: Math.max(0, analytics.total_bookings - analytics.confirmed - analytics.cancelled), color: '#f59e0b' },
    ] : []

    return (
      <div className="space-y-5">
        <div>
          <h1 className="text-lg font-semibold text-gray-900">Dashboard</h1>
          <p className="text-sm text-gray-400">System overview and analytics</p>
        </div>

        {loadingStats ? (
          <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="bg-white border border-gray-100 rounded-lg px-4 py-3 animate-pulse">
                <div className="h-2.5 bg-gray-100 rounded w-2/3 mb-2" />
                <div className="h-6 bg-gray-100 rounded w-1/2" />
              </div>
            ))}
          </div>
        ) : analytics ? (
          <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
            <StatCard label="Total Leads" value={analytics.total_leads} />
            <StatCard label="Bookings" value={analytics.total_bookings} />
            <StatCard label="Confirmed" value={analytics.confirmed} accent="text-green-600" />
            <StatCard label="Cancelled" value={analytics.cancelled} accent="text-red-500" />
            <StatCard label="Conversion" value={`${(analytics.conversion_rate * 100).toFixed(1)}%`} accent="text-blue-600" />
          </div>
        ) : null}

        <div className="bg-white border border-gray-100 rounded-lg p-4">
          <p className="text-xs font-semibold text-gray-600 mb-3">Booking Overview</p>
          {analytics ? (
            <ResponsiveContainer width="100%" height={150}>
              <BarChart data={chartData} barSize={26}>
                <XAxis dataKey="name" tick={{ fontSize: 10, fill: '#9ca3af' }} axisLine={false} tickLine={false} />
                <YAxis tick={{ fontSize: 10, fill: '#9ca3af' }} axisLine={false} tickLine={false} allowDecimals={false} width={20} />
                <Tooltip contentStyle={{ fontSize: 12, borderRadius: 6, border: '1px solid #f3f4f6' }} cursor={{ fill: '#f9fafb' }} />
                <Bar dataKey="value" radius={[3, 3, 0, 0]}>
                  {chartData.map((entry, i) => <Cell key={i} fill={entry.color} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <div className="h-36 flex items-center justify-center text-xs text-gray-400">No data yet</div>
          )}
        </div>
      </div>
    )
  }

  // Organizer dashboard — their bookings
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold text-gray-900">My Bookings</h1>
          <p className="text-sm text-gray-400">All meetings booked with you</p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => setViewMode('table')}
            className={`inline-flex items-center gap-2 px-3 py-1.5 rounded-md text-sm font-medium transition-colors ${
              viewMode === 'table'
                ? 'bg-blue-100 text-blue-700'
                : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
            }`}
          >
            <List className="w-4 h-4" /> Table
          </button>
          <button
            onClick={() => setViewMode('calendar')}
            className={`inline-flex items-center gap-2 px-3 py-1.5 rounded-md text-sm font-medium transition-colors ${
              viewMode === 'calendar'
                ? 'bg-blue-100 text-blue-700'
                : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
            }`}
          >
            <Calendar className="w-4 h-4" /> Calendar
          </button>
        </div>
      </div>

      {loadingBookings ? (
        <div className="bg-white border border-gray-100 rounded-lg p-4 text-sm text-gray-400">Loading…</div>
      ) : viewMode === 'calendar' ? (
        <BookingCalendar bookings={bookings || []} />
      ) : (
        <div className="bg-white border border-gray-100 rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[11px] text-gray-400 border-b border-gray-100 bg-gray-50">
                <th className="text-left px-4 py-2.5 font-medium">Lead Name</th>
                <th className="text-left px-4 py-2.5 font-medium">Email</th>
                <th className="text-left px-4 py-2.5 font-medium">Timezone</th>
                <th className="text-left px-4 py-2.5 font-medium">Status</th>
                <th className="text-left px-4 py-2.5 font-medium">Date / Time</th>
                <th className="text-left px-4 py-2.5 font-medium">Booking Link</th>
                <th className="px-4 py-2.5" />
              </tr>
            </thead>
            <tbody>
              {bookings && bookings.length > 0 ? (
                bookings.map((b) => (
                  <tr key={b.id} className="border-b border-gray-50 hover:bg-gray-50 transition-colors">
                    <td className="px-4 py-2.5 font-medium text-gray-800">{b.lead?.name ?? '—'}</td>
                    <td className="px-4 py-2.5 text-gray-600">{b.lead?.email ?? '—'}</td>
                    <td className="px-4 py-2.5 text-xs text-gray-500">
                      {b.lead?.timezone ?? <span className="text-gray-300">—</span>}
                    </td>
                    <td className="px-4 py-2.5">
                      <span className={stateBadge(b.state)}>{b.state.replace(/_/g, ' ')}</span>
                    </td>
                    <td className="px-4 py-2.5 text-xs text-gray-500">
                      {b.slot_start ? new Date(b.slot_start).toLocaleString() : '—'}
                    </td>
                    <td className="px-4 py-2.5 text-xs">
                      {b.booking_link
                        ? <a href={b.booking_link} target="_blank" rel="noreferrer" className="text-blue-600 hover:underline">View</a>
                        : <span className="text-gray-400">—</span>}
                    </td>
                    <td className="px-4 py-2.5 text-right text-xs text-gray-500">
                      {b.state === 'booking_confirmed' && (
                        <button className="text-red-500 hover:text-red-700 font-medium">Cancel</button>
                      )}
                    </td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={6} className="px-4 py-8 text-center text-sm text-gray-400">
                    No bookings yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
