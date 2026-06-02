import { useState, useEffect, FormEvent } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  getOrganizer,
  updateOrganizer,
  replaceAvailability,
  addHoliday,
  deleteHoliday,
} from '../api/client'
import type { Organizer, AvailabilityRule } from '../api/types'

const DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
const TIMEZONES = [
  'UTC', 'Asia/Kolkata', 'Asia/Dubai', 'Asia/Singapore', 'Asia/Tokyo',
  'Europe/London', 'Europe/Berlin', 'America/New_York', 'America/Chicago',
  'America/Denver', 'America/Los_Angeles', 'Australia/Sydney',
]

interface DayRule { enabled: boolean; start_time: string; end_time: string }

function rulesFromOrg(rules: AvailabilityRule[]): DayRule[] {
  return Array.from({ length: 7 }, (_, i) => {
    const r = rules.find((x) => x.weekday === i)
    return { enabled: !!r, start_time: r?.start_time?.slice(0, 5) ?? '09:00', end_time: r?.end_time?.slice(0, 5) ?? '17:00' }
  })
}

interface Props { organizerId: string; onBack: () => void }

export default function OrganizerForm({ organizerId, onBack }: Props) {
  const queryClient = useQueryClient()
  const { data: org, isLoading } = useQuery<Organizer>({
    queryKey: ['organizer', organizerId],
    queryFn: () => getOrganizer(organizerId).then((r) => r.data),
  })

  const [displayName, setDisplayName] = useState('')
  const [timezone, setTimezone] = useState('UTC')
  const [meetingMinutes, setMeetingMinutes] = useState(30)
  const [bufferMinutes, setBufferMinutes] = useState(0)
  const [horizonDays, setHorizonDays] = useState(14)
  const [meetingLink, setMeetingLink] = useState('')
  const [isActive, setIsActive] = useState(true)
  const [availability, setAvailability] = useState<DayRule[]>(Array.from({ length: 7 }, (_, i) => ({
    enabled: i < 5, start_time: '09:00', end_time: '17:00',
  })))
  const [holidayDay, setHolidayDay] = useState('')
  const [holidayLabel, setHolidayLabel] = useState('')
  const [saving, setSaving] = useState(false)
  const [savingAvail, setSavingAvail] = useState(false)
  const [msg, setMsg] = useState<{ type: 'ok' | 'err'; text: string } | null>(null)

  useEffect(() => {
    if (org) {
      setDisplayName(org.display_name)
      setTimezone(org.timezone)
      setMeetingMinutes(org.default_meeting_minutes)
      setBufferMinutes(org.buffer_minutes)
      setHorizonDays(org.booking_horizon_days)
      setMeetingLink(org.meeting_link ?? '')
      setIsActive(org.is_active)
      setAvailability(rulesFromOrg(org.availability_rules))
    }
  }, [org])

  async function saveSettings(e: FormEvent) {
    e.preventDefault()
    setSaving(true); setMsg(null)
    try {
      await updateOrganizer(organizerId, {
        display_name: displayName, timezone, default_meeting_minutes: meetingMinutes,
        buffer_minutes: bufferMinutes, booking_horizon_days: horizonDays,
        meeting_link: meetingLink || null, is_active: isActive,
      })
      queryClient.invalidateQueries({ queryKey: ['organizer', organizerId] })
      queryClient.invalidateQueries({ queryKey: ['organizers'] })
      setMsg({ type: 'ok', text: 'Settings saved.' })
    } catch { setMsg({ type: 'err', text: 'Save failed.' }) }
    finally { setSaving(false) }
  }

  async function saveAvailability() {
    setSavingAvail(true); setMsg(null)
    try {
      const rules = availability.map((d, i) => ({ ...d, weekday: i })).filter((d) => d.enabled)
        .map(({ weekday, start_time, end_time }) => ({ weekday, start_time, end_time }))
      await replaceAvailability(organizerId, rules)
      queryClient.invalidateQueries({ queryKey: ['organizer', organizerId] })
      setMsg({ type: 'ok', text: 'Availability saved.' })
    } catch { setMsg({ type: 'err', text: 'Failed.' }) }
    finally { setSavingAvail(false) }
  }

  const addHolidayMutation = useMutation({
    mutationFn: () => addHoliday(organizerId, { day: holidayDay, label: holidayLabel || undefined }),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['organizer', organizerId] }); setHolidayDay(''); setHolidayLabel('') },
  })

  const delHolidayMutation = useMutation({
    mutationFn: (hId: string) => deleteHoliday(organizerId, hId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['organizer', organizerId] }),
  })

  if (isLoading) return <p className="text-sm text-gray-400 py-4">Loading…</p>

  return (
    <div>
      <div className="flex items-center gap-3 mb-6">
        <button onClick={onBack} className="text-sm text-gray-400 hover:text-gray-600">← Back</button>
        <h1 className="text-xl font-semibold text-gray-900">{org?.display_name}</h1>
        <span className="text-xs text-gray-400">{org?.user?.email}</span>
      </div>

      {msg && (
        <div className={`mb-4 text-sm rounded-lg px-3 py-2 border ${msg.type === 'ok' ? 'text-green-700 bg-green-50 border-green-200' : 'text-red-600 bg-red-50 border-red-200'}`}>
          {msg.text}
        </div>
      )}

      {/* Settings */}
      <form onSubmit={saveSettings} className="bg-white border border-gray-200 rounded-xl p-5 mb-5">
        <h2 className="text-sm font-semibold text-gray-700 mb-4">General Settings</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <F label="Display Name"><input required value={displayName} onChange={(e) => setDisplayName(e.target.value)} className={inp} /></F>
          <F label="Timezone">
            <select value={timezone} onChange={(e) => setTimezone(e.target.value)} className={inp}>
              {TIMEZONES.map((tz) => <option key={tz}>{tz}</option>)}
            </select>
          </F>
          <F label="Meeting Duration (min)"><input type="number" min={5} value={meetingMinutes} onChange={(e) => setMeetingMinutes(+e.target.value)} className={inp} /></F>
          <F label="Buffer Between Meetings (min)"><input type="number" min={0} value={bufferMinutes} onChange={(e) => setBufferMinutes(+e.target.value)} className={inp} /></F>
          <F label="Booking Horizon (days)"><input type="number" min={1} value={horizonDays} onChange={(e) => setHorizonDays(+e.target.value)} className={inp} /></F>
          <F label="Meeting Link (auto-generated if empty)">
            <input type="url" value={meetingLink} onChange={(e) => setMeetingLink(e.target.value)} className={inp} placeholder="https://meet.google.com/…" />
          </F>
        </div>
        <div className="mt-4 flex items-center gap-2">
          <input type="checkbox" id="active" checked={isActive} onChange={(e) => setIsActive(e.target.checked)} className="h-4 w-4 rounded border-gray-300 text-blue-600" />
          <label htmlFor="active" className="text-sm text-gray-700">Active</label>
        </div>
        <div className="mt-5">
          <button type="submit" disabled={saving} className={btn}>{saving ? 'Saving…' : 'Save Settings'}</button>
        </div>
      </form>

      {/* Availability */}
      <div className="bg-white border border-gray-200 rounded-xl p-5 mb-5">
        <h2 className="text-sm font-semibold text-gray-700 mb-4">Weekly Availability</h2>
        <div className="space-y-2">
          {DAYS.map((day, i) => (
            <div key={day} className="flex items-center gap-3">
              <button type="button" onClick={() => setAvailability((p) => p.map((d, idx) => idx === i ? { ...d, enabled: !d.enabled } : d))}
                className={`w-14 text-xs font-medium py-1 rounded-full transition-colors ${availability[i].enabled ? 'bg-blue-600 text-white' : 'bg-gray-100 text-gray-400'}`}>
                {day.slice(0, 3)}
              </button>
              {availability[i].enabled ? (
                <>
                  <input type="time" value={availability[i].start_time}
                    onChange={(e) => setAvailability((p) => p.map((d, idx) => idx === i ? { ...d, start_time: e.target.value } : d))}
                    className="border border-gray-300 rounded-md px-2 py-1 text-sm" />
                  <span className="text-gray-400 text-sm">to</span>
                  <input type="time" value={availability[i].end_time}
                    onChange={(e) => setAvailability((p) => p.map((d, idx) => idx === i ? { ...d, end_time: e.target.value } : d))}
                    className="border border-gray-300 rounded-md px-2 py-1 text-sm" />
                </>
              ) : <span className="text-xs text-gray-400">Unavailable</span>}
            </div>
          ))}
        </div>
        <div className="mt-4">
          <button type="button" onClick={saveAvailability} disabled={savingAvail} className={btn}>
            {savingAvail ? 'Saving…' : 'Save Availability'}
          </button>
        </div>
      </div>

      {/* Holidays */}
      <div className="bg-white border border-gray-200 rounded-xl p-5">
        <h2 className="text-sm font-semibold text-gray-700 mb-4">Blocked Dates / Holidays</h2>
        <div className="space-y-1 mb-4">
          {org?.holidays.map((h) => (
            <div key={h.id} className="flex items-center justify-between py-1.5 border-b border-gray-50">
              <div>
                <span className="text-sm font-medium text-gray-800">{h.day}</span>
                {h.label && <span className="text-xs text-gray-500 ml-2">{h.label}</span>}
              </div>
              <button onClick={() => delHolidayMutation.mutate(h.id)} className="text-xs text-red-500 hover:text-red-700">Remove</button>
            </div>
          ))}
          {org?.holidays.length === 0 && <p className="text-xs text-gray-400 py-1">No blocked dates.</p>}
        </div>
        <div className="flex items-end gap-2 flex-wrap">
          <F label="Date"><input type="date" value={holidayDay} onChange={(e) => setHolidayDay(e.target.value)} className={inp} /></F>
          <F label="Label (optional)"><input value={holidayLabel} onChange={(e) => setHolidayLabel(e.target.value)} className={inp} placeholder="e.g. Public Holiday" /></F>
          <div className="pb-0.5">
            <button type="button" onClick={() => holidayDay && addHolidayMutation.mutate()} disabled={!holidayDay || addHolidayMutation.isPending} className={btn}>
              Add
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

function F({ label, children }: { label: string; children: React.ReactNode }) {
  return <div><label className="block text-xs font-medium text-gray-600 mb-1">{label}</label>{children}</div>
}

const inp = 'w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500'
const btn = 'bg-blue-600 text-white text-sm font-medium px-4 py-2 rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors'
