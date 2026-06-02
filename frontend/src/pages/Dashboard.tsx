import { useQuery } from '@tanstack/react-query'
import { getAnalytics, listBookings, getUserInfo } from '../api/client'
import type { AnalyticsSummary, BookingListItem } from '../api/types'
import { stateBadge } from '../lib/badges'

function StatCard({ label, value, sub }: { label: string; value: string | number; sub?: string }) {
  return (
    <div className="bg-white border border-gray-200 rounded-xl px-5 py-4">
      <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">{label}</p>
      <p className="text-2xl font-bold text-gray-900 mt-1">{value}</p>
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

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-xl font-semibold text-gray-900">
          {isAdmin ? 'Dashboard' : `My Dashboard`}
        </h1>
        {user && (
          <p className="text-sm text-gray-400 mt-0.5">
            Welcome back, {user.full_name}
            {!isAdmin && ' · your bookings only'}
          </p>
        )}
      </div>

      {loadingStats ? (
        <p className="text-sm text-gray-400">Loading stats…</p>
      ) : analytics ? (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-8">
          <StatCard label="Total Leads" value={analytics.total_leads} />
          <StatCard label="Bookings" value={analytics.total_bookings} />
          <StatCard label="Confirmed" value={analytics.confirmed} />
          <StatCard label="Cancelled" value={analytics.cancelled} />
          <StatCard
            label="Conversion"
            value={`${(analytics.conversion_rate * 100).toFixed(1)}%`}
          />
        </div>
      ) : null}

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
