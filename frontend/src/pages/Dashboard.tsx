import { useQuery } from '@tanstack/react-query'
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from 'recharts'
import { getAnalytics, listBookings, getUserInfo } from '../api/client'
import type { AnalyticsSummary, BookingListItem } from '../api/types'
import { stateBadge } from '../lib/badges'

function StatCard({ label, value, sub, accent }: { label: string; value: string | number; sub?: string; accent?: string }) {
  return (
    <div className="bg-white border border-gray-200 rounded-xl px-5 py-4">
      <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">{label}</p>
      <p className={`text-2xl font-bold mt-1 ${accent ?? 'text-gray-900'}`}>{value}</p>
      {sub && <p className="text-xs text-gray-400 mt-0.5">{sub}</p>}
    </div>
  )
}

export default function Dashboard() {
  const user = getUserInfo()
  const { data: analytics, isLoading: loadingStats } = useQuery<AnalyticsSummary>({
    queryKey: ['analytics'],
    queryFn: () => getAnalytics().then((r) => r.data),
  })

  const { data: bookings, isLoading: loadingBookings } = useQuery<BookingListItem[]>({
    queryKey: ['bookings', 0, 10],
    queryFn: () => listBookings(0, 10).then((r) => r.data),
  })

  const isAdmin = user?.role === 'admin'

  const chartData = analytics ? [
    { name: 'Total', value: analytics.total_bookings, color: '#3b82f6' },
    { name: 'Confirmed', value: analytics.confirmed, color: '#22c55e' },
    { name: 'Cancelled', value: analytics.cancelled, color: '#ef4444' },
    { name: 'Pending', value: Math.max(0, analytics.total_bookings - analytics.confirmed - analytics.cancelled), color: '#f59e0b' },
  ] : []

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-xl font-semibold text-gray-900">
          {isAdmin ? 'Dashboard' : 'My Dashboard'}
        </h1>
        {user && (
          <p className="text-sm text-gray-400 mt-0.5">
            Welcome back, {user.full_name}
            {!isAdmin && ' · your bookings only'}
          </p>
        )}
      </div>

      {/* Stat cards */}
      {loadingStats ? (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-6">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="bg-white border border-gray-200 rounded-xl px-5 py-4 animate-pulse">
              <div className="h-3 bg-gray-100 rounded w-2/3 mb-2" />
              <div className="h-7 bg-gray-100 rounded w-1/2" />
            </div>
          ))}
        </div>
      ) : analytics ? (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-6">
          <StatCard label="Total Leads" value={analytics.total_leads} />
          <StatCard label="Bookings" value={analytics.total_bookings} />
          <StatCard label="Confirmed" value={analytics.confirmed} accent="text-green-600" />
          <StatCard label="Cancelled" value={analytics.cancelled} accent="text-red-500" />
          <StatCard
            label="Conversion"
            value={`${(analytics.conversion_rate * 100).toFixed(1)}%`}
            accent="text-blue-600"
          />
        </div>
      ) : null}

      {/* Chart + Recent bookings */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-5 mb-6">
        {/* Bar chart */}
        <div className="bg-white border border-gray-200 rounded-xl p-5">
          <p className="text-sm font-semibold text-gray-700 mb-4">Booking Overview</p>
          {analytics ? (
            <ResponsiveContainer width="100%" height={160}>
              <BarChart data={chartData} barSize={28}>
                <XAxis dataKey="name" tick={{ fontSize: 11, fill: '#9ca3af' }} axisLine={false} tickLine={false} />
                <YAxis tick={{ fontSize: 11, fill: '#9ca3af' }} axisLine={false} tickLine={false} allowDecimals={false} width={24} />
                <Tooltip
                  contentStyle={{ fontSize: 12, borderRadius: 8, border: '1px solid #e5e7eb', boxShadow: '0 2px 8px rgba(0,0,0,0.08)' }}
                  cursor={{ fill: '#f9fafb' }}
                />
                <Bar dataKey="value" radius={[4, 4, 0, 0]}>
                  {chartData.map((entry, index) => (
                    <Cell key={index} fill={entry.color} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <div className="h-40 flex items-center justify-center text-sm text-gray-400">No data</div>
          )}
        </div>

        {/* Quick stats */}
        <div className="bg-white border border-gray-200 rounded-xl p-5 md:col-span-2">
          <p className="text-sm font-semibold text-gray-700 mb-4">At a Glance</p>
          <div className="space-y-3">
            {[
              { label: 'Confirmed rate', value: analytics ? `${(analytics.confirmed / (analytics.total_bookings || 1) * 100).toFixed(0)}%` : '—', bar: analytics ? analytics.confirmed / (analytics.total_bookings || 1) : 0, color: 'bg-green-500' },
              { label: 'Cancellation rate', value: analytics ? `${(analytics.cancelled / (analytics.total_bookings || 1) * 100).toFixed(0)}%` : '—', bar: analytics ? analytics.cancelled / (analytics.total_bookings || 1) : 0, color: 'bg-red-400' },
              { label: 'Lead conversion', value: analytics ? `${(analytics.conversion_rate * 100).toFixed(1)}%` : '—', bar: analytics ? analytics.conversion_rate : 0, color: 'bg-blue-500' },
            ].map(({ label, value, bar, color }) => (
              <div key={label}>
                <div className="flex justify-between text-xs text-gray-500 mb-1">
                  <span>{label}</span>
                  <span className="font-medium text-gray-700">{value}</span>
                </div>
                <div className="h-1.5 bg-gray-100 rounded-full overflow-hidden">
                  <div className={`h-full rounded-full ${color} transition-all`} style={{ width: `${Math.min(bar * 100, 100)}%` }} />
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Recent bookings table */}
      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden">
        <div className="px-5 py-3 border-b border-gray-100">
          <p className="text-sm font-semibold text-gray-700">Recent Bookings</p>
        </div>
        {loadingBookings ? (
          <p className="text-sm text-gray-400 px-5 py-4">Loading…</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-gray-500 border-b border-gray-100 bg-gray-50">
                <th className="text-left px-5 py-2 font-medium">Lead</th>
                {isAdmin && <th className="text-left px-5 py-2 font-medium">Organizer</th>}
                <th className="text-left px-5 py-2 font-medium">State</th>
                <th className="text-left px-5 py-2 font-medium">Slot</th>
              </tr>
            </thead>
            <tbody>
              {bookings?.map((b) => (
                <tr key={b.id} className="border-b border-gray-50 hover:bg-gray-50">
                  <td className="px-5 py-2.5">
                    <p className="font-medium text-gray-800">{b.lead?.name ?? '—'}</p>
                    <p className="text-xs text-gray-400">{b.lead?.email}</p>
                  </td>
                  {isAdmin && <td className="px-5 py-2.5 text-gray-600">{b.organizer?.display_name ?? '—'}</td>}
                  <td className="px-5 py-2.5">
                    <span className={stateBadge(b.state)}>{b.state.replace(/_/g, ' ')}</span>
                  </td>
                  <td className="px-5 py-2.5 text-gray-500">
                    {b.slot_start ? new Date(b.slot_start).toLocaleString() : '—'}
                  </td>
                </tr>
              ))}
              {bookings?.length === 0 && (
                <tr>
                  <td colSpan={isAdmin ? 4 : 3} className="px-5 py-6 text-center text-gray-400">
                    No bookings yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
