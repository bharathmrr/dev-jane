import type { BookingState } from '../api/types'

const stateColors: Record<BookingState, string> = {
  lead_created: 'bg-gray-100 text-gray-600',
  email_sent: 'bg-blue-100 text-blue-700',
  waiting_for_reply: 'bg-yellow-100 text-yellow-700',
  slot_selected: 'bg-orange-100 text-orange-700',
  slot_reserved: 'bg-orange-100 text-orange-700',
  booking_confirmed: 'bg-green-100 text-green-700',
  reminder_sent: 'bg-green-100 text-green-700',
  meeting_completed: 'bg-purple-100 text-purple-700',
  cancelled: 'bg-red-100 text-red-600',
  rescheduled: 'bg-yellow-100 text-yellow-700',
}

export function stateBadge(state: BookingState) {
  return `inline-block rounded-full px-2 py-0.5 text-xs font-medium ${stateColors[state] ?? 'bg-gray-100 text-gray-600'}`
}
