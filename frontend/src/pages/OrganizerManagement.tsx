import { useState, FormEvent } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ChevronsUpDownIcon, PlusIcon, AlertCircleIcon, UsersIcon } from 'lucide-react'
import { listOrganizers, updateOrganizer, createOrganizer } from '../api/client'
import type { Organizer } from '../api/types'
import OrganizerForm from './OrganizerForm'
import { Collapsible, CollapsibleTrigger, CollapsibleContent } from '@/components/ui/collapsible'
import { Alert, AlertTitle, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

function avatar(name: string) {
  return name.charAt(0).toUpperCase()
}

function AccessBadge({ role }: { role: string }) {
  if (role === 'admin')
    return <span className="text-xs bg-purple-100 text-purple-700 px-2 py-0.5 rounded-full font-medium">Admin</span>
  if (role === 'organizer')
    return <span className="text-xs bg-blue-100 text-blue-700 px-2 py-0.5 rounded-full font-medium">Organizer</span>
  return <span className="text-xs bg-gray-100 text-gray-500 px-2 py-0.5 rounded-full font-medium">Viewer</span>
}

export default function OrganizerManagement() {
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState<'all' | 'active' | 'inactive'>('all')
  const [showForm, setShowForm] = useState(false)
  const [actionOpen, setActionOpen] = useState<string | null>(null)
  const [editingId, setEditingId] = useState<string | null>(null)

  const queryClient = useQueryClient()

  const { data: organizers = [], isLoading } = useQuery<Organizer[]>({
    queryKey: ['organizers', search],
    queryFn: () => listOrganizers(search).then((r) => r.data),
  })

  const toggleActiveMutation = useMutation({
    mutationFn: ({ id, is_active }: { id: string; is_active: boolean }) =>
      updateOrganizer(id, { is_active }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['organizers'] }),
  })

  const changeAccessMutation = useMutation({
    mutationFn: ({ id, access_level }: { id: string; access_level: string }) =>
      updateOrganizer(id, { access_level }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['organizers'] }),
  })

  const filtered = organizers.filter((o) => {
    if (filter === 'active') return o.is_active
    if (filter === 'inactive') return !o.is_active
    return true
  })

  const activeCount = organizers.filter((o) => o.is_active).length

  if (editingId) {
    return <OrganizerForm organizerId={editingId} onBack={() => setEditingId(null)} />
  }

  return (
    <div>
      {/* Header */}
      <div className="flex items-start justify-between mb-6">
        <div>
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 bg-blue-100 rounded-lg flex items-center justify-center">
              <UsersIcon className="w-4 h-4 text-blue-600" />
            </div>
            <h1 className="text-xl font-bold text-gray-900">Organizer Management</h1>
          </div>
          <p className="text-sm text-gray-500 mt-1 ml-10">
            Manage organizers — availability, access level, and account status.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-sm text-gray-500 bg-gray-100 px-3 py-1.5 rounded-full">
            {organizers.length} total · {activeCount} active
          </span>
          <Button size="sm" onClick={() => setShowForm((v) => !v)}>
            <PlusIcon className="h-4 w-4" />
            Add Organizer
          </Button>
        </div>
      </div>

      {/* Collapsible create form */}
      <Collapsible open={showForm} onOpenChange={setShowForm}>
        <CollapsibleContent>
          <div className="bg-white border border-blue-200 rounded-xl p-5 mb-5 shadow-sm">
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-2">
                <PlusIcon className="h-4 w-4 text-blue-600" />
                <h2 className="text-sm font-semibold text-gray-700">New Organizer</h2>
              </div>
              <CollapsibleTrigger asChild>
                <Button variant="ghost" size="icon" className="h-7 w-7 text-gray-400 hover:text-gray-600">
                  <ChevronsUpDownIcon className="h-4 w-4" />
                </Button>
              </CollapsibleTrigger>
            </div>
            <OrganizerCreateInline
              onCreated={() => {
                setShowForm(false)
                queryClient.invalidateQueries({ queryKey: ['organizers'] })
              }}
            />
          </div>
        </CollapsibleContent>
      </Collapsible>

      {/* Search + Filter */}
      <div className="flex items-center gap-3 mb-4">
        <div className="relative flex-1">
          <svg className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
          </svg>
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search by name or email…"
            className="w-full border border-gray-200 rounded-xl pl-9 pr-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white"
          />
        </div>
        <div className="flex gap-1 bg-gray-100 rounded-lg p-1">
          {(['all', 'active', 'inactive'] as const).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`text-xs font-medium px-3 py-1.5 rounded-md transition-colors capitalize ${
                filter === f ? 'bg-white text-gray-800 shadow-sm' : 'text-gray-500 hover:text-gray-700'
              }`}
            >
              {f}
            </button>
          ))}
        </div>
      </div>

      {/* Table */}
      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-gray-500 border-b border-gray-100 bg-gray-50/80">
              <th className="text-left px-5 py-3 font-medium">ORGANIZER</th>
              <th className="text-left px-5 py-3 font-medium">AVAILABILITY</th>
              <th className="text-left px-5 py-3 font-medium">STATUS</th>
              <th className="text-left px-5 py-3 font-medium">ACCESS</th>
              <th className="px-5 py-3 font-medium text-right">ACTIONS</th>
            </tr>
          </thead>
          <tbody>
            {isLoading && (
              <tr><td colSpan={5} className="px-5 py-8 text-center text-gray-400">Loading…</td></tr>
            )}
            {!isLoading && filtered.length === 0 && (
              <tr><td colSpan={5} className="px-5 py-8 text-center text-gray-400">No organizers found.</td></tr>
            )}
            {filtered.map((org) => (
              <tr key={org.id} className="border-b border-gray-50 hover:bg-gray-50/50 transition-colors">
                <td className="px-5 py-3.5">
                  <div className="flex items-center gap-3">
                    <div className="w-8 h-8 rounded-full bg-blue-100 text-blue-700 flex items-center justify-center text-sm font-semibold shrink-0">
                      {avatar(org.display_name)}
                    </div>
                    <div>
                      <p className="font-medium text-gray-900">{org.display_name}</p>
                      <p className="text-xs text-gray-400">{org.user?.email ?? '—'}</p>
                    </div>
                  </div>
                </td>

                <td className="px-5 py-3.5">
                  <div className="flex gap-0.5 flex-wrap">
                    {DAYS.map((d, i) => {
                      const active = org.availability_rules.some((r) => r.weekday === i)
                      return (
                        <span key={d} className={`text-xs px-1 py-0.5 rounded font-medium ${
                          active ? 'bg-blue-100 text-blue-700' : 'text-gray-300'
                        }`}>{d}</span>
                      )
                    })}
                  </div>
                  <p className="text-xs text-gray-400 mt-0.5">{org.timezone}</p>
                </td>

                <td className="px-5 py-3.5">
                  <button
                    onClick={() => toggleActiveMutation.mutate({ id: org.id, is_active: !org.is_active })}
                    className={`flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-full transition-colors ${
                      org.is_active
                        ? 'bg-green-100 text-green-700 hover:bg-green-200'
                        : 'bg-gray-100 text-gray-500 hover:bg-gray-200'
                    }`}
                  >
                    <span className={`w-1.5 h-1.5 rounded-full ${org.is_active ? 'bg-green-500' : 'bg-gray-400'}`} />
                    {org.is_active ? 'Active' : 'Inactive'}
                  </button>
                </td>

                <td className="px-5 py-3.5">
                  {org.user?.role === 'admin' ? (
                    <AccessBadge role="admin" />
                  ) : (
                    <select
                      value={org.user?.role === 'organizer' ? 'organizer' : 'viewer'}
                      onChange={(e) => changeAccessMutation.mutate({ id: org.id, access_level: e.target.value })}
                      className="border border-gray-200 rounded-lg px-2 py-1 text-xs text-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white"
                    >
                      <option value="organizer">Organizer</option>
                      <option value="viewer">Viewer (read-only)</option>
                    </select>
                  )}
                </td>

                <td className="px-5 py-3.5 text-right">
                  <div className="relative inline-block">
                    <button
                      onClick={() => setActionOpen(actionOpen === org.id ? null : org.id)}
                      className="w-8 h-8 rounded-full border border-gray-200 text-gray-500 hover:bg-gray-100 flex items-center justify-center text-base transition-colors"
                    >
                      ···
                    </button>
                    {actionOpen === org.id && (
                      <div className="absolute right-0 top-9 z-10 bg-white border border-gray-200 rounded-lg shadow-lg py-1 w-44">
                        <button
                          onClick={() => { setEditingId(org.id); setActionOpen(null) }}
                          className="w-full text-left px-4 py-2 text-sm text-gray-700 hover:bg-gray-50"
                        >
                          Edit settings
                        </button>
                        <button
                          onClick={() => {
                            toggleActiveMutation.mutate({ id: org.id, is_active: !org.is_active })
                            setActionOpen(null)
                          }}
                          className="w-full text-left px-4 py-2 text-sm text-gray-700 hover:bg-gray-50"
                        >
                          {org.is_active ? 'Deactivate' : 'Activate'}
                        </button>
                      </div>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {actionOpen && (
        <div className="fixed inset-0 z-5" onClick={() => setActionOpen(null)} />
      )}
    </div>
  )
}

// --- Inline create form ---
function OrganizerCreateInline({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [timezone, setTimezone] = useState('UTC')
  const [access, setAccess] = useState<'organizer' | 'viewer'>('organizer')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const TIMEZONES = [
    'UTC', 'Asia/Kolkata', 'Asia/Dubai', 'Asia/Singapore', 'Asia/Tokyo',
    'Europe/London', 'Europe/Berlin', 'America/New_York', 'America/Los_Angeles',
  ]

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await createOrganizer({
        display_name: name, email, password, timezone, access_level: access,
        rules: [
          { weekday: 0, start_time: '09:00', end_time: '17:00' },
          { weekday: 1, start_time: '09:00', end_time: '17:00' },
          { weekday: 2, start_time: '09:00', end_time: '17:00' },
          { weekday: 3, start_time: '09:00', end_time: '17:00' },
          { weekday: 4, start_time: '09:00', end_time: '17:00' },
        ],
      })
      onCreated()
    } catch (err: any) {
      setError(err.response?.data?.detail ?? 'Failed to create organizer.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <form onSubmit={handleSubmit}>
      {error && (
        <Alert variant="destructive" className="mb-3">
          <AlertCircleIcon className="h-4 w-4" />
          <AlertTitle>Error</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      <div className="grid grid-cols-2 md:grid-cols-3 gap-3 mb-3">
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">Full Name</label>
          <input required value={name} onChange={(e) => setName(e.target.value)} className={inp} placeholder="Jane Smith" />
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">Email</label>
          <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} className={inp} placeholder="jane@company.com" />
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">Password</label>
          <input type="password" required minLength={6} value={password} onChange={(e) => setPassword(e.target.value)} className={inp} placeholder="min 6 chars" />
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">Timezone</label>
          <select value={timezone} onChange={(e) => setTimezone(e.target.value)} className={inp}>
            {TIMEZONES.map((tz) => <option key={tz} value={tz}>{tz}</option>)}
          </select>
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">Access Level</label>
          <select value={access} onChange={(e) => setAccess(e.target.value as 'organizer' | 'viewer')} className={inp}>
            <option value="organizer">Organizer (full access)</option>
            <option value="viewer">Viewer (read-only)</option>
          </select>
        </div>
      </div>
      <p className="text-xs text-gray-400 mb-3">Availability defaults to Mon–Fri 9–5. Edit after creation.</p>
      <Button type="submit" disabled={loading} size="sm">
        {loading ? 'Creating…' : 'Create Organizer'}
      </Button>
    </form>
  )
}

const inp = 'w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500'
