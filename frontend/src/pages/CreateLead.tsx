import { useState, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { CheckCircle2Icon, AlertCircleIcon, SendIcon, EyeIcon, ArrowLeftIcon, MailIcon } from 'lucide-react'
import { listOrganizers, createLead, previewLeadEmail, getUserInfo } from '../api/client'
import type { Organizer } from '../api/types'
import { Alert, AlertTitle, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'

type Step = 'form' | 'preview' | 'sent'

interface Preview {
  subject: string
  body: string
  from_email: string
  to_email: string
  organizer_name: string
  slots_count: number
}

export default function CreateLead() {
  const user = getUserInfo()
  const isAdmin = user?.role === 'admin'

  const { data: organizers } = useQuery<Organizer[]>({
    queryKey: ['organizers'],
    queryFn: () => listOrganizers().then((r) => r.data),
    enabled: isAdmin,
  })

  const [step, setStep] = useState<Step>('form')
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [organizerId, setOrganizerId] = useState(
    !isAdmin && user?.organizer_id ? user.organizer_id : ''
  )
  const [preview, setPreview] = useState<Preview | null>(null)
  const [editedBody, setEditedBody] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!isAdmin && user?.organizer_id) setOrganizerId(user.organizer_id)
  }, [user, isAdmin])

  const active = (organizers ?? []).filter((o) => o.is_active)

  // Step 1 → 2: fetch preview
  async function handlePreview() {
    if (!name.trim() || !email.trim() || !organizerId) {
      setError('Fill in all fields first.')
      return
    }
    setError('')
    setLoading(true)
    try {
      const { data } = await previewLeadEmail({ lead_name: name, lead_email: email, organizer_id: organizerId })
      setPreview(data)
      setEditedBody(data.body)
      setStep('preview')
    } catch (err: any) {
      setError(err.response?.data?.detail ?? 'Could not load preview. Make sure the organizer has availability set up.')
    } finally {
      setLoading(false)
    }
  }

  // Step 2 → 3: actually send
  async function handleSend() {
    setLoading(true)
    setError('')
    try {
      await createLead({ name, email, organizer_id: organizerId })
      setStep('sent')
    } catch (err: any) {
      setError(err.response?.data?.detail ?? 'Failed to send email.')
    } finally {
      setLoading(false)
    }
  }

  function reset() {
    setStep('form')
    setName('')
    setEmail('')
    setOrganizerId(!isAdmin && user?.organizer_id ? user.organizer_id : '')
    setPreview(null)
    setEditedBody('')
    setError('')
  }

  return (
    <div className="max-w-2xl">
      <div className="mb-6">
        <h1 className="text-xl font-semibold text-gray-900">Send Scheduling Email</h1>
        <p className="text-sm text-gray-400 mt-0.5">
          Preview the email before it goes out — you can edit the body if needed.
        </p>
      </div>

      {/* Step indicator */}
      <div className="flex items-center gap-2 mb-6">
        {(['form', 'preview', 'sent'] as Step[]).map((s, i) => (
          <div key={s} className="flex items-center gap-2">
            <div className={`w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold transition-colors ${
              step === s ? 'bg-blue-600 text-white' :
              (['preview', 'sent'].includes(step) && s === 'form') || (step === 'sent' && s === 'preview')
                ? 'bg-green-500 text-white' : 'bg-gray-100 text-gray-400'
            }`}>{i + 1}</div>
            <span className={`text-xs font-medium capitalize ${step === s ? 'text-blue-600' : 'text-gray-400'}`}>
              {s === 'form' ? 'Details' : s === 'preview' ? 'Preview & Edit' : 'Sent'}
            </span>
            {i < 2 && <span className="text-gray-200 mx-1">›</span>}
          </div>
        ))}
      </div>

      {error && (
        <Alert variant="destructive" className="mb-4">
          <AlertCircleIcon className="h-4 w-4" />
          <AlertTitle>Error</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {/* ── STEP 1: Form ── */}
      {step === 'form' && (
        <div className="bg-white border border-gray-200 rounded-xl p-6 space-y-4">
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Lead Name</label>
            <input
              required value={name}
              onChange={(e) => setName(e.target.value)}
              className={inp} placeholder="John Doe"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Lead Email</label>
            <input
              type="email" required value={email}
              onChange={(e) => setEmail(e.target.value)}
              className={inp} placeholder="john@example.com"
            />
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
                <Alert variant="warning" className="mt-2">
                  <AlertCircleIcon className="h-4 w-4" />
                  <AlertDescription>No active organizers. Create one in Organizer Management first.</AlertDescription>
                </Alert>
              )}
            </div>
          ) : (
            <div className="bg-blue-50 border border-blue-100 rounded-lg px-3 py-2.5">
              <p className="text-xs text-blue-600 font-medium">Sending as: {user?.full_name}</p>
              <p className="text-xs text-blue-400 mt-0.5">The lead will receive your available time slots.</p>
            </div>
          )}

          <Button
            onClick={handlePreview}
            disabled={loading || !name || !email || !organizerId}
            className="w-full"
          >
            {loading ? 'Loading preview…' : <><EyeIcon className="h-4 w-4" /> Preview Email</>}
          </Button>
        </div>
      )}

      {/* ── STEP 2: Preview & Edit ── */}
      {step === 'preview' && preview && (
        <div className="space-y-4">
          {/* Email meta */}
          <div className="bg-white border border-gray-200 rounded-xl p-5 space-y-2">
            <div className="flex items-center gap-2 text-sm">
              <span className="text-gray-400 w-12 text-right shrink-0">From</span>
              <span className="bg-gray-50 border border-gray-100 rounded px-2 py-0.5 text-gray-700 font-mono text-xs">{preview.from_email}</span>
            </div>
            <div className="flex items-center gap-2 text-sm">
              <span className="text-gray-400 w-12 text-right shrink-0">To</span>
              <span className="bg-gray-50 border border-gray-100 rounded px-2 py-0.5 text-gray-700 font-mono text-xs">{preview.to_email}</span>
            </div>
            <div className="flex items-center gap-2 text-sm">
              <span className="text-gray-400 w-12 text-right shrink-0">Subject</span>
              <span className="text-gray-800 font-medium text-xs">{preview.subject}</span>
            </div>
            <div className="border-t border-gray-100 pt-2 flex items-center gap-2 text-xs text-gray-400">
              <MailIcon className="h-3.5 w-3.5" />
              {preview.slots_count} available slots · organizer: {preview.organizer_name}
            </div>
          </div>

          {/* Editable body */}
          <div className="bg-white border border-gray-200 rounded-xl p-5">
            <div className="flex items-center justify-between mb-2">
              <label className="text-xs font-medium text-gray-600">Email Body</label>
              <span className="text-xs text-gray-400">You can edit this before sending</span>
            </div>
            <textarea
              value={editedBody}
              onChange={(e) => setEditedBody(e.target.value)}
              rows={14}
              className="w-full font-mono text-sm text-gray-700 border border-gray-200 rounded-lg p-3 focus:outline-none focus:ring-2 focus:ring-blue-500 resize-y bg-gray-50"
            />
          </div>

          <div className="flex items-center gap-3">
            <Button variant="outline" onClick={() => { setStep('form'); setError('') }} className="gap-2">
              <ArrowLeftIcon className="h-4 w-4" /> Back
            </Button>
            <Button onClick={handleSend} disabled={loading} className="flex-1">
              {loading ? 'Sending…' : <><SendIcon className="h-4 w-4" /> Send Email Now</>}
            </Button>
          </div>
        </div>
      )}

      {/* ── STEP 3: Sent ── */}
      {step === 'sent' && (
        <div className="bg-white border border-gray-200 rounded-xl p-8 text-center">
          <div className="w-14 h-14 bg-green-100 rounded-full flex items-center justify-center mx-auto mb-4">
            <CheckCircle2Icon className="h-7 w-7 text-green-600" />
          </div>
          <h2 className="text-lg font-semibold text-gray-900 mb-1">Email Sent!</h2>
          <p className="text-sm text-gray-500 mb-1">
            Scheduling email delivered to <span className="font-medium text-gray-700">{email}</span>
          </p>
          <p className="text-xs text-gray-400 mb-6">
            When they reply choosing a slot, the system will auto-confirm and send a meeting link.
          </p>
          <div className="flex items-center justify-center gap-3">
            <Button onClick={reset}>Send Another</Button>
            <Button variant="outline" onClick={() => window.location.href = '/bookings'}>
              View Bookings
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}

const inp = 'w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500'
