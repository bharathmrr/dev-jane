import { Calendar, momentLocalizer } from 'react-big-calendar'
import moment from 'moment'
import type { BookingListItem } from '../api/types'
import 'react-big-calendar/lib/css/react-big-calendar.css'

const localizer = momentLocalizer(moment)

interface BookingCalendarProps {
  bookings: BookingListItem[]
  onSelectSlot?: (start: Date, end: Date) => void
}

export default function BookingCalendar({ bookings, onSelectSlot }: BookingCalendarProps) {
  const events = bookings
    .filter(b => b.slot_start && b.state === 'booking_confirmed')
    .map(b => ({
      id: b.id,
      title: `${b.lead?.name} - Booked`,
      start: new Date(b.slot_start!),
      end: new Date(b.slot_end || b.slot_start!),
      resource: b,
    }))

  return (
    <div style={{ height: '500px' }} className="bg-white border border-gray-100 rounded-lg overflow-hidden">
      <Calendar
        localizer={localizer}
        events={events}
        startAccessor="start"
        endAccessor="end"
        style={{ height: '100%' }}
        popup
        onSelectSlot={onSelectSlot ? ({ start, end }) => onSelectSlot(start, end) : undefined}
        selectable={!!onSelectSlot}
        defaultView="month"
        views={['month', 'week', 'day']}
        defaultDate={new Date()}
        eventPropGetter={() => ({
          className: 'bg-red-100 text-red-700',
        })}
      />
    </div>
  )
}
