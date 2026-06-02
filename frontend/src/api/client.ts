import axios from 'axios'
import type { AvailabilityRuleIn, Me, Organizer } from './types'

const api = axios.create({ baseURL: '/api/v1' })

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('access_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

api.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err.response?.status === 401) {
      localStorage.removeItem('access_token')
      localStorage.removeItem('user_info')
      window.location.href = '/login'
    }
    return Promise.reject(err)
  },
)

export default api

// Auth
export const login = (email: string, password: string) =>
  api.post('/auth/login', { email, password })

export const getMe = () => api.get<Me>('/auth/me')

// Analytics (scoped by role server-side)
export const getAnalytics = () => api.get('/analytics/summary')

// Organizers (admin only)
export const listOrganizers = (search = '') =>
  api.get<Organizer[]>('/admin/organizers', { params: search ? { search } : {} })
export const getOrganizer = (id: string) => api.get<Organizer>(`/admin/organizers/${id}`)
export const createOrganizer = (data: object) => api.post<Organizer>('/admin/organizers', data)
export const updateOrganizer = (id: string, data: object) =>
  api.put<Organizer>(`/admin/organizers/${id}`, data)
export const replaceAvailability = (id: string, rules: AvailabilityRuleIn[]) =>
  api.put<Organizer>(`/admin/organizers/${id}/availability`, rules)
export const addHoliday = (id: string, data: { day: string; label?: string }) =>
  api.post(`/admin/organizers/${id}/holidays`, data)
export const deleteHoliday = (id: string, holidayId: string) =>
  api.delete(`/admin/organizers/${id}/holidays/${holidayId}`)

// My organizer profile (for organizer role)
export const getMyOrganizer = () => api.get<Organizer>('/me/organizer')

// Bookings (scoped by role server-side)
export const listBookings = (skip = 0, limit = 50) =>
  api.get('/bookings/list', { params: { skip, limit } })
export const cancelBooking = (id: string) => api.post(`/bookings/${id}/cancel`)

// Leads
export const createLead = (data: object) => api.post('/leads', data)
export const previewLeadEmail = (data: { lead_name: string; lead_email: string; organizer_id: string }) =>
  api.post<{ subject: string; body: string; from_email: string; to_email: string; organizer_name: string; slots_count: number }>('/leads/preview', data)
export const listSlots = (organizerId: string) =>
  api.get(`/organizers/${organizerId}/slots`)

// User helpers
export function getUserInfo(): Me | null {
  const raw = localStorage.getItem('user_info')
  if (!raw) return null
  try { return JSON.parse(raw) } catch { return null }
}

export function setUserInfo(info: Me) {
  localStorage.setItem('user_info', JSON.stringify(info))
}

export function clearSession() {
  localStorage.removeItem('access_token')
  localStorage.removeItem('user_info')
}
