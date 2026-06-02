import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import api, { getUserInfo } from '../api/client'
import type { BookingListItem } from '../api/types'

interface Lead {
  id: string
  name: string
  email: string
  created_at: string
  organizer?: { display_name: string }
  booking?: BookingListItem
}

export default function Leads() {
  const [search, setSearch] = useState('')
  const user = getUserInfo()
  const isAdmin = user?.role === 'admin'

  const { data: leads, isLoading } = useQuery<Lead[]>({
    queryKey: ['leads', search],
    queryFn: async () => {
      const res = await api.get('/leads', { params: search ? { search } : {} })
      return res.data
    },
  })

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold text-gray-900">{isAdmin ? 'All Leads' : 'My Leads'}</h1>
        <p className="text-sm text-gray-400">{isAdmin ? 'Leads from all organizers' : 'Leads assigned to you'}</p>
      </div>

      <div className="bg-white border border-gray-100 rounded-lg p-4">
        <input
          type="text"
          placeholder="Search by name or email..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
      </div>

      <div className="bg-white border border-gray-100 rounded-lg overflow-hidden">
        {isLoading ? (
          <p className="px-4 py-4 text-sm text-gray-400">Loading...</p>
        ) : leads && leads.length > 0 ? (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[11px] text-gray-400 border-b border-gray-100 bg-gray-50">
                <th className="text-left px-4 py-2.5 font-medium">Name</th>
                <th className="text-left px-4 py-2.5 font-medium">Email</th>
                <th className="text-left px-4 py-2.5 font-medium">Organizer</th>
                <th className="text-left px-4 py-2.5 font-medium">Status</th>
                <th className="text-left px-4 py-2.5 font-medium">Date</th>
              </tr>
            </thead>
            <tbody>
              {leads.map((lead) => (
                <tr key={lead.id} className="border-b border-gray-50 hover:bg-gray-50 transition-colors">
                  <td className="px-4 py-2.5 font-medium text-gray-800">{lead.name}</td>
                  <td className="px-4 py-2.5 text-gray-600">{lead.email}</td>
                  <td className="px-4 py-2.5 text-sm text-gray-600">
                    {lead.organizer?.display_name || '—'}
                  </td>
                  <td className="px-4 py-2.5 text-xs">
                    {lead.booking?.state ? (
                      <span className="bg-blue-100 text-blue-700 px-2 py-1 rounded-full">
                        {lead.booking.state.replace(/_/g, ' ')}
                      </span>
                    ) : (
                      <span className="text-gray-400">New</span>
                    )}
                  </td>
                  <td className="px-4 py-2.5 text-xs text-gray-500">
                    {new Date(lead.created_at).toLocaleDateString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="px-4 py-8 text-center text-sm text-gray-400">No leads found.</p>
        )}
      </div>
    </div>
  )
}
