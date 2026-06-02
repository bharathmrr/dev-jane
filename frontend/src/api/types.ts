export type UserRole = 'admin' | 'organizer' | 'agent'

export type BookingState =
  | 'lead_created'
  | 'email_sent'
  | 'waiting_for_reply'
  | 'slot_selected'
  | 'slot_reserved'
  | 'booking_confirmed'
  | 'reminder_sent'
  | 'meeting_completed'
  | 'cancelled'
  | 'rescheduled'

export interface Token {
  access_token: string
  refresh_token: string
  token_type: string
}

export interface Me {
  id: string
  email: string
  full_name: string
  role: UserRole
  is_active: boolean
  organizer_id: string | null
}

export interface AvailabilityRule {
  id: string
  weekday: number
  start_time: string
  end_time: string
}

export interface Holiday {
  id: string
  day: string
  label: string | null
}

export interface UserInOrganizer {
  id: string
  email: string
  role: UserRole
}

export interface Organizer {
  id: string
  display_name: string
  timezone: string
  default_meeting_minutes: number
  buffer_minutes: number
  booking_horizon_days: number
  meeting_link: string | null
  is_active: boolean
  availability_rules: AvailabilityRule[]
  holidays: Holiday[]
  user: UserInOrganizer | null
}

export interface BookingLead {
  id: string
  name: string
  email: string
  timezone?: string | null
}

export interface BookingOrganizer {
  id: string
  display_name: string
  email?: string
}

export interface BookingListItem {
  id: string
  state: BookingState
  organizer_id: string
  lead_id: string
  slot_start: string | null
  slot_end: string | null
  booking_link: string | null
  lead: BookingLead | null
  organizer: BookingOrganizer | null
}

export interface AnalyticsSummary {
  total_leads: number
  total_bookings: number
  confirmed: number
  cancelled: number
  conversion_rate: number
}

export interface SlotOut {
  start: string
  end: string
  label: string
}

export interface AvailabilityRuleIn {
  weekday: number
  start_time: string
  end_time: string
}
