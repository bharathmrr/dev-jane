import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { listOrganizers } from '../api/client'
import type { Organizer } from '../api/types'

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

export default function Organizers() {
  const { data: organizers, isLoading } = useQuery<Organizer[]>({
    queryKey: ['organizers'],
    queryFn: () => listOrganizers().then((r) => r.data),
  })

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-xl font-semibold text-gray-900">Organizers</h1>
        <Link
          to="/organizers/new"
          className="bg-blue-600 text-white text-sm font-medium px-4 py-2 rounded-lg hover:bg-blue-700 transition-colors"
        >
          + New Organizer
        </Link>
      </div>

      {isLoading && <p className="text-sm text-gray-400">Loading…</p>}

      <div className="space-y-3">
        {organizers?.map((org) => (
          <div
            key={org.id}
            className="bg-white border border-gray-200 rounded-xl px-5 py-4 flex items-start justify-between"
          >
            <div>
              <div className="flex items-center gap-2">
                <p className="font-semibold text-gray-900">{org.display_name}</p>
                {!org.is_active && (
                  <span className="text-xs bg-gray-100 text-gray-500 rounded-full px-2 py-0.5">
                    Inactive
                  </span>
                )}
              </div>
              <p className="text-xs text-gray-500 mt-0.5">
                {org.timezone} · {org.default_meeting_minutes} min ·{' '}
                {org.booking_horizon_days} day horizon
              </p>
              {org.meeting_link && (
                <p className="text-xs text-blue-600 mt-1 truncate max-w-sm">{org.meeting_link}</p>
              )}
              <div className="flex gap-1 mt-2">
                {DAYS.map((day, i) => {
                  const active = org.availability_rules.some((r) => r.weekday === i)
                  return (
                    <span
                      key={day}
                      className={`text-xs px-1.5 py-0.5 rounded font-medium ${
                        active ? 'bg-blue-100 text-blue-700' : 'bg-gray-100 text-gray-400'
                      }`}
                    >
                      {day}
                    </span>
                  )
                })}
              </div>
              {org.holidays.length > 0 && (
                <p className="text-xs text-gray-400 mt-1">
                  {org.holidays.length} holiday{org.holidays.length > 1 ? 's' : ''}
                </p>
              )}
            </div>
            <Link
              to={`/organizers/${org.id}`}
              className="text-sm text-blue-600 hover:text-blue-800 font-medium ml-4 shrink-0"
            >
              Edit →
            </Link>
          </div>
        ))}

        {!isLoading && organizers?.length === 0 && (
          <div className="text-center py-12 text-gray-400">
            No organizers yet.{' '}
            <Link to="/organizers/new" className="text-blue-600 hover:underline">
              Create one.
            </Link>
          </div>
        )}
      </div>
    </div>
  )
}
