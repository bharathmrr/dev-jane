import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { XCircle } from 'lucide-react'
import { listBookings, cancelBooking, getUserInfo } from '../api/client'
import type { BookingListItem } from '../api/types'
import { stateBadge } from '../lib/badges'

const CANCELLABLE = ['booking_confirmed', 'reminder_sent', 'slot_reserved', 'waiting_for_reply', 'email_sent']

export default function Bookings() {
  const [page, setPage] = useState(0)
  const limit = 20
  const queryClient = useQueryClient()
  const user = getUserInfo()
  const isAdmin = user?.role === 'admin'
  const isViewer = user?.role === 'agent'

  const { data: bookings, isLoading } = useQuery<BookingListItem[]>({
    queryKey: ['bookings-list', page],
    queryFn: () => listBookings(page * limit, limit).then((r) => r.data),
  })

  const cancelMutation = useMutation({
    mutationFn: (id: string) => cancelBooking(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['bookings-list'] }),
  })

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold text-gray-900">{isAdmin ? 'All Bookings' : 'My Bookings'}</h1>
        {!isAdmin && <p className="text-sm text-gray-400">Bookings for your schedule only</p>}
      </div>

      <div className="bg-white border border-gray-100 rounded-lg overflow-hidden">
        {isLoading ? (
          <p className="text-sm text-gray-400 px-4 py-4">Loading…</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[11px] text-gray-400 border-b border-gray-100 bg-gray-50">
                <th className="text-left px-4 py-2.5 font-medium">Lead</th>
                {isAdmin && <th className="text-left px-4 py-2.5 font-medium">Organizer</th>}
                <th className="text-left px-4 py-2.5 font-medium">Status</th>
                <th className="text-left px-4 py-2.5 font-medium">Slot</th>
                {!isViewer && <th className="px-4 py-2.5" />}
              </tr>
            </thead>
            <tbody>
              {bookings?.map((b) => (
                <tr key={b.id} className="border-b border-gray-50 hover:bg-gray-50 transition-colors">
                  <td className="px-4 py-2.5">
                    <p className="font-medium text-gray-800">{b.lead?.name ?? '—'}</p>
                    <p className="text-xs text-gray-400">{b.lead?.email}</p>
                  </td>
                  {isAdmin && <td className="px-4 py-2.5 text-gray-600">{b.organizer?.display_name ?? '—'}</td>}
                  <td className="px-4 py-2.5">
                    <span className={stateBadge(b.state)}>{b.state.replace(/_/g, ' ')}</span>
                  </td>
                  <td className="px-4 py-2.5 text-xs text-gray-500">
                    {b.slot_start ? new Date(b.slot_start).toLocaleString() : '—'}
                  </td>
                  {!isViewer && (
                    <td className="px-4 py-2.5 text-right">
                      {CANCELLABLE.includes(b.state) && (
                        <button
                          onClick={() => { if (confirm('Cancel this booking?')) cancelMutation.mutate(b.id) }}
                          className="inline-flex items-center gap-1 text-xs text-red-500 hover:text-red-700 font-medium"
                        >
                          <XCircle className="w-3.5 h-3.5" /> Cancel
                        </button>
                      )}
                    </td>
                  )}
                </tr>
              ))}
              {bookings?.length === 0 && (
                <tr>
                  <td colSpan={isAdmin ? 5 : isViewer ? 3 : 4} className="px-4 py-8 text-center text-sm text-gray-400">
                    No bookings found.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>

      <div className="flex items-center justify-between">
        <button disabled={page === 0} onClick={() => setPage(p => p - 1)}
          className="text-xs text-gray-500 hover:text-gray-700 disabled:opacity-40 font-medium">← Previous</button>
        <span className="text-xs text-gray-400">Page {page + 1}</span>
        <button disabled={(bookings?.length ?? 0) < limit} onClick={() => setPage(p => p + 1)}
          className="text-xs text-gray-500 hover:text-gray-700 disabled:opacity-40 font-medium">Next →</button>
      </div>
    </div>
  )
}
