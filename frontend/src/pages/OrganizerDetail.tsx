import { useState, useEffect, FormEvent } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  getOrganizer,
  createOrganizer,
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

interface DayRule {
  enabled: boolean
  start_time: string
  end_time: string
}

function rulesFromOrganizer(rules: AvailabilityRule[]): DayRule[] {
  return Array.from({ length: 7 }, (_, i) => {
    const r = rules.find((x) => x.weekday === i)
    return {
      enabled: !!r,
      start_time: r?.start_time?.slice(0, 5) ?? '09:00',
      end_time: r?.end_time?.slice(0, 5) ?? '17:00',
    }
  })
}

export default function OrganizerDetail() {
  const { id } = useParams<{ id: string }>()
  const isNew = !id
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const { data: org, isLoading } = useQuery<Organizer>({
    queryKey: ['organizer', id],
    queryFn: () => getOrganizer(id!).then((r) => r.data),
    enabled: !isNew,
  })

  // Settings form
  const [displayName, setDisplayName] = useState('')
  const [orgEmail, setOrgEmail] = useState('')
  const [timezone, setTimezone] = useState('UTC')
  const [meetingMinutes, setMeetingMinutes] = useState(30)
  const [bufferMinutes, setBufferMinutes] = useState(0)
  const [horizonDays, setHorizonDays] = useState(14)
  const [meetingLink, setMeetingLink] = useState('')
  const [isActive, setIsActive] = useState(true)

  // Availability
  const [availability, setAvailability] = useState<DayRule[]>(
    Array.from({ length: 7 }, (_, i) => ({
      enabled: i < 5,
      start_time: '09:00',
      end_time: '17:00',
    })),
  )

  // Holidays
  const [holidayDay, setHolidayDay] = useState('')
  const [holidayLabel, setHolidayLabel] = useState('')

  const [saving, setSaving] = useState(false)
  const [savingAvail, setSavingAvail] = useState(false)
  const [error, setError] = useState('')
  const [successMsg, setSuccessMsg] = useState('')

  useEffect(() => {
    if (org) {
      setDisplayName(org.display_name)
      setTimezone(org.timezone)
      setMeetingMinutes(org.default_meeting_minutes)
      setBufferMinutes(org.buffer_minutes)
      setHorizonDays(org.booking_horizon_days)
      setMeetingLink(org.meeting_link ?? '')
      setIsActive(org.is_active)
      setAvailability(rulesFromOrganizer(org.availability_rules))
    }
  }, [org])

  async function saveSettings(e: FormEvent) {
    e.preventDefault()
    setSaving(true)
    setError('')
    setSuccessMsg('')
    try {
      const payload = {
        display_name: displayName,
        timezone,
        default_meeting_minutes: meetingMinutes,
        buffer_minutes: bufferMinutes,
        booking_horizon_days: horizonDays,
        meeting_link: meetingLink || null,
        is_active: isActive,
      }
      if (isNew) {
        const rules = availability
          .map((d, i) => ({ ...d, weekday: i }))
          .filter((d) => d.enabled)
          .map(({ weekday, start_time, end_time }) => ({ weekday, start_time, end_time }))
        const { data } = await createOrganizer({ ...payload, email: orgEmail, rules })
        queryClient.invalidateQueries({ queryKey: ['organizers'] })
        navigate(`/organizers/${data.id}`)
      } else {
        await updateOrganizer(id!, payload)
        queryClient.invalidateQueries({ queryKey: ['organizer', id] })
        queryClient.invalidateQueries({ queryKey: ['organizers'] })
        setSuccessMsg('Settings saved.')
      }
    } catch (err: any) {
      setError(err.response?.data?.detail ?? 'Save failed.')
    } finally {
      setSaving(false)
    }
  }

  async function saveAvailability() {
    setSavingAvail(true)
    setError('')
    try {
      const rules = availability
        .map((d, i) => ({ ...d, weekday: i }))
        .filter((d) => d.enabled)
        .map(({ weekday, start_time, end_time }) => ({ weekday, start_time, end_time }))
      await replaceAvailability(id!, rules)
      queryClient.invalidateQueries({ queryKey: ['organizer', id] })
      setSuccessMsg('Availability saved.')
    } catch {
      setError('Failed to save availability.')
    } finally {
      setSavingAvail(false)
    }
  }

  const addHolidayMutation = useMutation({
    mutationFn: () => addHoliday(id!, { day: holidayDay, label: holidayLabel || undefined }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['organizer', id] })
      setHolidayDay('')
      setHolidayLabel('')
    },
  })

  const deleteHolidayMutation = useMutation({
    mutationFn: (hId: string) => deleteHoliday(id!, hId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['organizer', id] }),
  })

  function toggleDay(i: number) {
    setAvailability((prev) => prev.map((d, idx) => (idx === i ? { ...d, enabled: !d.enabled } : d)))
  }

  function updateDayTime(i: number, field: 'start_time' | 'end_time', value: string) {
    setAvailability((prev) => prev.map((d, idx) => (idx === i ? { ...d, [field]: value } : d)))
  }

  if (!isNew && isLoading) {
    return <p className="text-sm text-gray-400">Loading…</p>
  }

  return (
    <div>
      <div className="flex items-center gap-3 mb-6">
        <Link to="/organizers" className="text-sm text-gray-400 hover:text-gray-600">
          ← Organizers
        </Link>
        <h1 className="text-xl font-semibold text-gray-900">
          {isNew ? 'New Organizer' : org?.display_name}
        </h1>
      </div>

      {error && (
        <div className="mb-4 text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
          {error}
        </div>
      )}
      {successMsg && (
        <div className="mb-4 text-sm text-green-600 bg-green-50 border border-green-200 rounded-lg px-3 py-2">
          {successMsg}
        </div>
      )}

      {/* Settings */}
      <form onSubmit={saveSettings} className="bg-white border border-gray-200 rounded-xl p-5 mb-5">
        <h2 className="text-sm font-semibold text-gray-700 mb-4">General Settings</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <Field label="Display Name">
            <input
              required
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              className={input}
              placeholder="e.g. Bharath R"
            />
          </Field>
          {isNew && (
            <Field label="Organizer Email">
              <input
                type="email"
                required
                value={orgEmail}
                onChange={(e) => setOrgEmail(e.target.value)}
                className={input}
                placeholder="organizer@company.com"
              />
            </Field>
          )}
          <Field label="Timezone">
            <select value={timezone} onChange={(e) => setTimezone(e.target.value)} className={input}>
              {TIMEZONES.map((tz) => (
                <option key={tz} value={tz}>{tz}</option>
              ))}
            </select>
          </Field>
          <Field label="Meeting Duration (minutes)">
            <input
              type="number"
              min={5}
              value={meetingMinutes}
              onChange={(e) => setMeetingMinutes(+e.target.value)}
              className={input}
            />
          </Field>
          <Field label="Buffer Between Meetings (minutes)">
            <input
              type="number"
              min={0}
              value={bufferMinutes}
              onChange={(e) => setBufferMinutes(+e.target.value)}
              className={input}
            />
          </Field>
          <Field label="Booking Horizon (days)">
            <input
              type="number"
              min={1}
              value={horizonDays}
              onChange={(e) => setHorizonDays(+e.target.value)}
              className={input}
            />
          </Field>
          <Field label="Meeting Link (Zoom / Meet URL)">
            <input
              type="url"
              value={meetingLink}
              onChange={(e) => setMeetingLink(e.target.value)}
              className={input}
              placeholder="https://meet.google.com/abc-def"
            />
          </Field>
        </div>
        {!isNew && (
          <div className="mt-4 flex items-center gap-2">
            <input
              type="checkbox"
              id="active"
              checked={isActive}
              onChange={(e) => setIsActive(e.target.checked)}
              className="h-4 w-4 rounded border-gray-300 text-blue-600"
            />
            <label htmlFor="active" className="text-sm text-gray-700">Active</label>
          </div>
        )}
        <div className="mt-5">
          <button type="submit" disabled={saving} className={btn}>
            {saving ? 'Saving…' : isNew ? 'Create Organizer' : 'Save Settings'}
          </button>
        </div>
      </form>

      {/* Availability — only shown after creation */}
      {!isNew && (
        <>
          <div className="bg-white border border-gray-200 rounded-xl p-5 mb-5">
            <h2 className="text-sm font-semibold text-gray-700 mb-4">Weekly Availability</h2>
            <div className="space-y-2">
              {DAYS.map((day, i) => (
                <div key={day} className="flex items-center gap-3">
                  <button
                    type="button"
                    onClick={() => toggleDay(i)}
                    className={`w-14 text-xs font-medium py-1 rounded-full transition-colors ${
                      availability[i].enabled
                        ? 'bg-blue-600 text-white'
                        : 'bg-gray-100 text-gray-400'
                    }`}
                  >
                    {day.slice(0, 3)}
                  </button>
                  {availability[i].enabled ? (
                    <>
                      <input
                        type="time"
                        value={availability[i].start_time}
                        onChange={(e) => updateDayTime(i, 'start_time', e.target.value)}
                        className="border border-gray-300 rounded-md px-2 py-1 text-sm"
                      />
                      <span className="text-gray-400 text-sm">to</span>
                      <input
                        type="time"
                        value={availability[i].end_time}
                        onChange={(e) => updateDayTime(i, 'end_time', e.target.value)}
                        className="border border-gray-300 rounded-md px-2 py-1 text-sm"
                      />
                    </>
                  ) : (
                    <span className="text-xs text-gray-400">Unavailable</span>
                  )}
                </div>
              ))}
            </div>
            <div className="mt-4">
              <button
                type="button"
                onClick={saveAvailability}
                disabled={savingAvail}
                className={btn}
              >
                {savingAvail ? 'Saving…' : 'Save Availability'}
              </button>
            </div>
          </div>

          {/* Holidays */}
          <div className="bg-white border border-gray-200 rounded-xl p-5">
            <h2 className="text-sm font-semibold text-gray-700 mb-4">Holidays / Blocked Dates</h2>
            <div className="space-y-1 mb-4">
              {org?.holidays.map((h) => (
                <div key={h.id} className="flex items-center justify-between py-1.5 border-b border-gray-50">
                  <div>
                    <span className="text-sm font-medium text-gray-800">{h.day}</span>
                    {h.label && <span className="text-xs text-gray-500 ml-2">{h.label}</span>}
                  </div>
                  <button
                    onClick={() => deleteHolidayMutation.mutate(h.id)}
                    className="text-xs text-red-500 hover:text-red-700"
                  >
                    Remove
                  </button>
                </div>
              ))}
              {org?.holidays.length === 0 && (
                <p className="text-xs text-gray-400 py-1">No holidays added.</p>
              )}
            </div>
            <div className="flex items-end gap-2">
              <Field label="Date">
                <input
                  type="date"
                  value={holidayDay}
                  onChange={(e) => setHolidayDay(e.target.value)}
                  className={input}
                />
              </Field>
              <Field label="Label (optional)">
                <input
                  value={holidayLabel}
                  onChange={(e) => setHolidayLabel(e.target.value)}
                  className={input}
                  placeholder="e.g. National Holiday"
                />
              </Field>
              <div className="pb-0.5">
                <button
                  type="button"
                  onClick={() => holidayDay && addHolidayMutation.mutate()}
                  disabled={!holidayDay || addHolidayMutation.isPending}
                  className={`${btn} whitespace-nowrap`}
                >
                  Add
                </button>
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-xs font-medium text-gray-600 mb-1">{label}</label>
      {children}
    </div>
  )
}

const input =
  'w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500'

const btn =
  'bg-blue-600 text-white text-sm font-medium px-4 py-2 rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors'
