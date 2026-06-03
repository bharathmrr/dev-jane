import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Calendar, List, RefreshCw, Mail, CheckCircle, Clock,
  CalendarDays, Trash2, ToggleLeft, ToggleRight, ExternalLink, Plus,
} from 'lucide-react'
import api, { getUserInfo } from '../api/client'
import type { LeadV2, AvailableDateSlot, ZohoSlotV2 } from '../api/types'

// ---------------------------------------------------------------------------
// Shared UI helpers
// ---------------------------------------------------------------------------

function StatCard({ label, value, accent }: { label: string; value: string | number; accent?: string }) {
  return (
    <div className="bg-white border border-gray-100 rounded-lg px-4 py-3 shadow-sm hover:shadow-md transition-shadow">
      <p className="text-[11px] font-semibold text-gray-500 uppercase tracking-wider">{label}</p>
      <p className={`text-3xl font-bold mt-1 ${accent ?? 'text-gray-900'}`}>{value}</p>
    </div>
  )
}

function statusBadge(state: string) {
  const map: Record<string, string> = {
    NEW:     'px-2 py-1 bg-gray-100 text-gray-700 text-xs font-semibold rounded-full',
    SENT:    'px-2 py-1 bg-blue-100 text-blue-700 text-xs font-semibold rounded-full',
    REPLIED: 'px-2 py-1 bg-yellow-100 text-yellow-700 text-xs font-semibold rounded-full',
    BOOKED:  'px-2 py-1 bg-green-100 text-green-700 text-xs font-semibold rounded-full',
    FAILED:  'px-2 py-1 bg-red-100 text-red-700 text-xs font-semibold rounded-full',
  }
  return map[state] ?? map.NEW
}

// ---------------------------------------------------------------------------
// Available-slots calendar view (group slots by date, show a week grid)
// ---------------------------------------------------------------------------

function CalendarView({ slots }: { slots: AvailableDateSlot[] }) {
  const [weekOffset, setWeekOffset] = useState(0)

  const today = new Date()
  today.setHours(0, 0, 0, 0)
  const startOfWeek = new Date(today)
  startOfWeek.setDate(today.getDate() - today.getDay() + weekOffset * 7)

  const days = Array.from({ length: 7 }, (_, i) => {
    const d = new Date(startOfWeek)
    d.setDate(startOfWeek.getDate() + i)
    return d
  })

  const slotsByDate: Record<string, AvailableDateSlot[]> = {}
  slots.forEach(s => {
    const key = s.slot_datetime.slice(0, 10)
    slotsByDate[key] = [...(slotsByDate[key] ?? []), s]
  })

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <button onClick={() => setWeekOffset(w => w - 1)}
          className="px-3 py-1 text-sm border border-gray-200 rounded-md hover:bg-gray-50">← Prev</button>
        <span className="text-sm font-medium text-gray-700">
          {startOfWeek.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} –{' '}
          {days[6].toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })}
        </span>
        <button onClick={() => setWeekOffset(w => w + 1)}
          className="px-3 py-1 text-sm border border-gray-200 rounded-md hover:bg-gray-50">Next →</button>
      </div>

      <div className="grid grid-cols-7 gap-2">
        {days.map(day => {
          const key = day.toISOString().slice(0, 10)
          const daySlots = slotsByDate[key] ?? []
          const isToday = day.getTime() === today.getTime()
          return (
            <div key={key}
              className={`rounded-lg border p-2 min-h-[90px] ${isToday ? 'border-blue-400 bg-blue-50' : 'border-gray-200 bg-white'}`}>
              <p className={`text-xs font-bold mb-1 ${isToday ? 'text-blue-600' : 'text-gray-500'}`}>
                {day.toLocaleDateString(undefined, { weekday: 'short' })}
                <br />
                <span className="text-base text-gray-900">{day.getDate()}</span>
              </p>
              <div className="space-y-1">
                {daySlots.map(s => (
                  <div key={s.id}
                    className={`text-[10px] px-1 py-0.5 rounded font-medium truncate ${s.is_available ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-400 line-through'}`}>
                    {new Date(s.slot_datetime).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                  </div>
                ))}
                {daySlots.length === 0 && (
                  <p className="text-[10px] text-gray-300 mt-2 text-center">—</p>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main Dashboard
// ---------------------------------------------------------------------------

export default function Dashboard() {
  const user = getUserInfo()
  const isAdmin = user?.role === 'admin'
  const qc = useQueryClient()

  const [tab, setTab] = useState<'leads' | 'slots' | 'dates'>('leads')
  const [calendarView, setCalendarView] = useState(false)
  const [newDate, setNewDate] = useState('')
  const [newTime, setNewTime] = useState('09:00')

  // ------ queries ------
  const { data: stats, isLoading: loadingStats } = useQuery({
    queryKey: ['v2-analytics'],
    queryFn: () => api.get('/v2/dashboard/stats').then(r => r.data),
    refetchInterval: 30_000,
  })
  const { data: leads, isLoading: loadingLeads } = useQuery<LeadV2[]>({
    queryKey: ['v2-leads'],
    queryFn: () => api.get('/v2/leads').then(r => r.data),
    refetchInterval: 30_000,
  })
  const { data: slots, isLoading: loadingSlots } = useQuery<ZohoSlotV2[]>({
    queryKey: ['v2-slots'],
    queryFn: () => api.get('/v2/slots').then(r => r.data),
    refetchInterval: 30_000,
  })
  const { data: availSlots = [], isLoading: loadingDates } = useQuery<AvailableDateSlot[]>({
    queryKey: ['v2-available-dates'],
    queryFn: () => api.get('/v2/available-dates').then(r => r.data),
  })

  // ------ mutations ------
  const addSlotMutation = useMutation({
    mutationFn: (slot_datetime: string) => api.post('/v2/available-dates', { slot_datetime }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['v2-available-dates'] }); setNewDate(''); setNewTime('09:00') },
    onError: (e: any) => alert(e?.response?.data?.detail ?? 'Failed to add slot'),
  })
  const toggleMutation = useMutation({
    mutationFn: (id: string) => api.patch(`/v2/available-dates/${id}/toggle`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['v2-available-dates'] }),
  })
  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.delete(`/v2/available-dates/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['v2-available-dates'] }),
  })
  const syncSheetsMutation = useMutation({
    mutationFn: () => api.post('/v2/sync-sheet'),
    onSuccess: () => alert('Sheets sync triggered'),
  })
  const processLeadsMutation = useMutation({
    mutationFn: () => api.post('/v2/send-email'),
    onSuccess: () => alert('Lead processing triggered'),
  })

  const handleAddSlot = () => {
    if (!newDate || !newTime) return
    addSlotMutation.mutate(`${newDate}T${newTime}:00`)
  }

  if (!isAdmin) {
    return <div className="p-4 text-gray-500">Only admins can view the V2 dashboard.</div>
  }

  return (
    <div className="space-y-6 pb-12">
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 tracking-tight">Lead Automation System</h1>
          <p className="text-sm text-gray-500 mt-1">Real-time scheduling &amp; lead management</p>
        </div>
        <div className="flex gap-2">
          <button onClick={() => syncSheetsMutation.mutate()} disabled={syncSheetsMutation.isPending}
            className="inline-flex items-center gap-2 px-4 py-2 bg-white border border-gray-200 text-gray-700 text-sm font-medium rounded-lg hover:bg-gray-50 shadow-sm">
            <RefreshCw className={`w-4 h-4 ${syncSheetsMutation.isPending ? 'animate-spin' : ''}`} />
            Sync Sheets
          </button>
          <button onClick={() => processLeadsMutation.mutate()} disabled={processLeadsMutation.isPending}
            className="inline-flex items-center gap-2 px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 shadow-sm">
            <Mail className={`w-4 h-4 ${processLeadsMutation.isPending ? 'animate-spin' : ''}`} />
            Process Leads
          </button>
        </div>
      </div>

      {/* Stat cards */}
      {loadingStats ? (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="bg-white border border-gray-100 rounded-lg px-4 py-3 animate-pulse h-24" />
          ))}
        </div>
      ) : stats ? (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
          <StatCard label="Total Leads"  value={stats.total_leads} />
          <StatCard label="Sent"         value={stats.sent_leads}    accent="text-blue-600" />
          <StatCard label="Replied"      value={stats.replied_leads} accent="text-yellow-600" />
          <StatCard label="Booked"       value={stats.booked_leads}  accent="text-green-600" />
          <StatCard label="Conversion"   value={`${(stats.conversion_rate * 100).toFixed(1)}%`} accent="text-indigo-600" />
        </div>
      ) : null}

      {/* Tabs */}
      <div className="flex gap-2 border-b border-gray-200">
        {([
          { key: 'leads',  icon: List,        label: 'Leads' },
          { key: 'slots',  icon: Calendar,    label: 'Zoho Slots' },
          { key: 'dates',  icon: CalendarDays, label: 'Booking Slots' },
        ] as const).map(({ key, icon: Icon, label }) => (
          <button key={key} onClick={() => setTab(key)}
            className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${tab === key ? 'border-blue-600 text-blue-600' : 'border-transparent text-gray-500 hover:text-gray-700'}`}>
            <Icon className="w-4 h-4 inline mr-2" />
            {label}
          </button>
        ))}
      </div>

      {/* ---- Leads table ---- */}
      {tab === 'leads' && (
        <div className="bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
          <div className="px-5 py-4 border-b border-gray-100 bg-gray-50/50 flex justify-between items-center">
            <h2 className="text-sm font-bold text-gray-800">Lead Pipeline</h2>
            <span className="text-xs text-gray-400">Live updates every 30s</span>
          </div>
          <div className="overflow-x-auto">
            {loadingLeads ? (
              <p className="px-5 py-8 text-sm text-gray-400 text-center">Loading leads…</p>
            ) : leads && leads.length > 0 ? (
              <table className="w-full text-sm text-left">
                <thead className="bg-gray-50/50 text-gray-500 text-xs uppercase font-semibold">
                  <tr>
                    <th className="px-5 py-3">Business</th>
                    <th className="px-5 py-3">Email</th>
                    <th className="px-5 py-3">Status</th>
                    <th className="px-5 py-3">Sent</th>
                    <th className="px-5 py-3">Booked Slot</th>
                    <th className="px-5 py-3">Meeting</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {leads.map(lead => (
                    <tr key={lead.id} className="hover:bg-gray-50/50 transition-colors">
                      <td className="px-5 py-3 font-medium text-gray-900">{lead.business_name}</td>
                      <td className="px-5 py-3 text-gray-600">{lead.email}</td>
                      <td className="px-5 py-3">
                        <span className={statusBadge(lead.status)}>{lead.status}</span>
                      </td>
                      <td className="px-5 py-3 text-gray-500 text-xs">
                        {lead.sent_at ? new Date(lead.sent_at).toLocaleString() : '—'}
                      </td>
                      <td className="px-5 py-3 text-gray-700 text-xs font-medium">
                        {lead.selected_slot ? (
                          <div className="flex items-center gap-1 text-green-700">
                            <CheckCircle className="w-3 h-3" />
                            {lead.selected_slot}
                          </div>
                        ) : '—'}
                      </td>
                      <td className="px-5 py-3">
                        {lead.zoho_meeting_link ? (
                          <a href={lead.zoho_meeting_link} target="_blank" rel="noopener noreferrer"
                            className="inline-flex items-center gap-1 text-blue-600 hover:underline text-xs font-medium">
                            <ExternalLink className="w-3 h-3" /> Join
                          </a>
                        ) : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="px-5 py-12 text-center text-sm text-gray-400">No leads in the system.</p>
            )}
          </div>
        </div>
      )}

      {/* ---- Zoho Slots table ---- */}
      {tab === 'slots' && (
        <div className="bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
          <div className="px-5 py-4 border-b border-gray-100 bg-gray-50/50">
            <h2 className="text-sm font-bold text-gray-800">Zoho Live Slots</h2>
          </div>
          <div className="overflow-x-auto">
            {loadingSlots ? (
              <p className="px-5 py-8 text-sm text-gray-400 text-center">Loading…</p>
            ) : slots && slots.length > 0 ? (
              <table className="w-full text-sm text-left">
                <thead className="bg-gray-50/50 text-gray-500 text-xs uppercase font-semibold">
                  <tr>
                    <th className="px-5 py-3">Slot Time</th>
                    <th className="px-5 py-3">Status</th>
                    <th className="px-5 py-3">Booked By</th>
                    <th className="px-5 py-3">Zoho ID</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {slots.map(slot => (
                    <tr key={slot.id} className="hover:bg-gray-50/50">
                      <td className="px-5 py-3 font-medium text-gray-900">
                        <div className="flex items-center gap-2">
                          <Clock className="w-4 h-4 text-blue-500" />
                          {new Date(slot.slot_time).toLocaleString()}
                        </div>
                      </td>
                      <td className="px-5 py-3">
                        <span className={`px-2 py-1 text-xs font-semibold rounded-full ${slot.status === 'AVAILABLE' ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}`}>
                          {slot.status}
                        </span>
                      </td>
                      <td className="px-5 py-3 text-gray-600">{slot.booked_email ?? '—'}</td>
                      <td className="px-5 py-3 text-gray-400 font-mono text-xs">{slot.zoho_slot_id}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="px-5 py-12 text-center text-sm text-gray-400">No Zoho slots synced yet.</p>
            )}
          </div>
        </div>
      )}

      {/* ---- Booking Slots (admin-managed) ---- */}
      {tab === 'dates' && (
        <div className="bg-white border border-gray-200 rounded-xl shadow-sm p-6 space-y-6">
          <div className="flex items-start justify-between">
            <div>
              <h2 className="text-lg font-bold text-gray-800">Booking Slots</h2>
              <p className="text-sm text-gray-500 mt-1">
                Add 1-hour slots to offer leads. Toggle to pause a slot without deleting it.
              </p>
            </div>
            <button
              onClick={() => setCalendarView(v => !v)}
              className="inline-flex items-center gap-2 px-3 py-1.5 border border-gray-200 text-sm rounded-lg hover:bg-gray-50">
              {calendarView ? <List className="w-4 h-4" /> : <CalendarDays className="w-4 h-4" />}
              {calendarView ? 'List view' : 'Calendar view'}
            </button>
          </div>

          {/* Add slot form */}
          <div className="flex flex-wrap gap-3 items-end p-4 bg-gray-50 rounded-xl border border-gray-200">
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Date</label>
              <input type="date" value={newDate} onChange={e => setNewDate(e.target.value)}
                className="px-3 py-2 border border-gray-300 rounded-md text-sm focus:ring-blue-500 focus:border-blue-500" />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Start time (1-hr slot)</label>
              <input type="time" value={newTime} onChange={e => setNewTime(e.target.value)}
                className="px-3 py-2 border border-gray-300 rounded-md text-sm focus:ring-blue-500 focus:border-blue-500" />
            </div>
            <button
              onClick={handleAddSlot}
              disabled={!newDate || !newTime || addSlotMutation.isPending}
              className="inline-flex items-center gap-2 px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 disabled:opacity-50">
              <Plus className="w-4 h-4" />
              Add Slot
            </button>
          </div>

          {loadingDates ? (
            <p className="text-sm text-gray-400">Loading slots…</p>
          ) : calendarView ? (
            <CalendarView slots={availSlots} />
          ) : (
            /* List view */
            availSlots.length > 0 ? (
              <div className="border border-gray-200 rounded-xl overflow-hidden">
                <table className="w-full text-sm text-left">
                  <thead className="bg-gray-50 text-gray-500 text-xs uppercase font-semibold">
                    <tr>
                      <th className="px-5 py-3">Slot</th>
                      <th className="px-5 py-3">Date &amp; Time</th>
                      <th className="px-5 py-3">Status</th>
                      <th className="px-5 py-3 text-right">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100">
                    {availSlots.map((slot, idx) => (
                      <tr key={slot.id} className="hover:bg-gray-50/50 transition-colors">
                        <td className="px-5 py-3 text-gray-400 font-mono text-xs">#{idx + 1}</td>
                        <td className="px-5 py-3 font-medium text-gray-900">
                          <div className="flex items-center gap-2">
                            <Clock className="w-4 h-4 text-blue-400 shrink-0" />
                            {slot.display}
                          </div>
                        </td>
                        <td className="px-5 py-3">
                          <span className={`px-2 py-1 text-xs font-semibold rounded-full ${slot.is_available ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'}`}>
                            {slot.is_available ? 'Available' : 'Paused'}
                          </span>
                        </td>
                        <td className="px-5 py-3 text-right">
                          <div className="flex justify-end gap-2">
                            <button
                              onClick={() => toggleMutation.mutate(slot.id)}
                              title={slot.is_available ? 'Pause slot' : 'Enable slot'}
                              className="p-1.5 rounded-md hover:bg-gray-100 text-gray-500 hover:text-blue-600 transition-colors">
                              {slot.is_available
                                ? <ToggleRight className="w-5 h-5 text-green-600" />
                                : <ToggleLeft className="w-5 h-5 text-gray-400" />}
                            </button>
                            <button
                              onClick={() => { if (confirm('Delete this slot?')) deleteMutation.mutate(slot.id) }}
                              title="Delete slot"
                              className="p-1.5 rounded-md hover:bg-red-50 text-gray-400 hover:text-red-600 transition-colors">
                              <Trash2 className="w-4 h-4" />
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="py-12 text-center text-sm text-gray-400 border border-dashed border-gray-200 rounded-xl">
                No slots added yet. Use the form above to add available booking times.
              </div>
            )
          )}
        </div>
      )}
    </div>
  )
}
