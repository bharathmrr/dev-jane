import { useState, FormEvent, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { listOrganizers, createLead, getUserInfo } from '../api/client'
import type { Organizer } from '../api/types'

export default function CreateLead() {
  const user = getUserInfo()
  const isAdmin = user?.role === 'admin'

  const { data: organizers } = useQuery<Organizer[]>({
    queryKey: ['organizers'],
    queryFn: () => listOrganizers().then((r) => r.data),
    enabled: isAdmin, // only admin needs the full list
  })

  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  // If organizer role, pre-fill with their own organizer_id
  const [organizerId, setOrganizerId] = useState(
    !isAdmin && user?.organizer_id ? user.organizer_id : ''
  )
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  useEffect(() => {
    if (!isAdmin && user?.organizer_id) {
      setOrganizerId(user.organizer_id)
    }
  }, [user, isAdmin])

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!organizerId) { setError('Select an organizer.'); return }
    setError(''); setSuccess(''); setLoading(true)
    try {
      await createLead({ name, email, organizer_id: organizerId })
      setSuccess(`Scheduling email sent to ${email}!`)
      setName(''); setEmail('')
    } catch (err: any) {
      setError(err.response?.data?.detail ?? 'Failed to create lead.')
    } finally {
      setLoading(false)
    }
  }

  const active = (organizers ?? []).filter((o) => o.is_active)

  return (
    <div>
      <h1 className="text-xl font-semibold text-gray-900 mb-6">Send Scheduling Email</h1>

      <div className="bg-white border border-gray-200 rounded-xl p-6 max-w-lg">
        <p className="text-sm text-gray-500 mb-5">
          Creates a lead and sends an email offering available time slots.
        </p>

        {error && (
          <div className="mb-4 text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">{error}</div>
        )}
        {success && (
          <div className="mb-4 text-sm text-green-700 bg-green-50 border border-green-200 rounded-lg px-3 py-2">{success}</div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Lead Name</label>
            <input required value={name} onChange={(e) => setName(e.target.value)} className={inp} placeholder="John Doe" />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Lead Email</label>
            <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} className={inp} placeholder="john@example.com" />
          </div>

          {isAdmin ? (
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Organizer</label>
              <select value={organizerId} onChange={(e) => setOrganizerId(e.target.value)} className={inp}>
                <option value="">Select organizer…</option>
                {active.map((o) => (
                  <option key={o.id} value={o.id}>{o.display_name} ({o.timezone})</option>
                ))}
              </select>
              {active.length === 0 && (
                <p className="text-xs text-amber-600 mt-1">No active organizers. Create one first.</p>
              )}
            </div>
          ) : (
            <div className="bg-blue-50 border border-blue-100 rounded-lg px-3 py-2.5">
              <p className="text-xs text-blue-600 font-medium">Sending as: you ({user?.full_name})</p>
              <p className="text-xs text-blue-400 mt-0.5">The lead will receive your available slots.</p>
            </div>
          )}

          <button type="submit" disabled={loading || (!organizerId)}
            className="w-full bg-blue-600 text-white text-sm font-medium py-2 rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors">
            {loading ? 'Sending…' : 'Send Scheduling Email'}
          </button>
        </form>
      </div>
    </div>
  )
}

const inp = 'w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500'
