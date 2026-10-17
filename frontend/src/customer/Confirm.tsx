import { useEffect, useState } from 'react'
import { loadPublic } from '../lib/firebase'
import { peso } from '../lib/format'

type Summary = {
  status: string
  items: { name: string; variant: string | null; quantity: number; unit_price: number | null }[]
  total: number | null
  recipient_name: string | null
  phone: string | null
  address: string | null
  delivery_instructions: string | null
  payment: 'pending' | 'verified' | 'mismatched' | 'expired' | null
  test_mode: boolean
  gcash_confirmed: boolean
  shipment: { status: string; courier: string; simulated: boolean; share_link: string | null } | null
  received: boolean
  claims: { id: string; type: string; status: string; created_at: string; photos: number }[]
}

const SHIPMENT: Record<string, string> = {
  booked: 'Booked with the courier',
  driver_assigned: 'A driver is on the way to pick it up',
  picked_up: 'On its way to you',
  delivered: 'The courier marked it delivered',
  cancelled: 'Delivery cancelled; the seller will contact you',
  failed: 'Delivery failed; the seller will contact you',
}
const PROBLEMS = {
  missing_item: 'Something is missing',
  wrong_item: 'I got the wrong item',
  damage: 'It arrived damaged',
  nonreceipt: 'I did not receive it',
  payment_issue: 'A problem with my payment',
  cancellation: 'I want to cancel',
  other: 'Something else',
}

/** Delivery status, "I received it", and reporting a problem. A report goes to the seller, who reviews and replies. */
function AfterPayment({ token, order, setOrder }: { token: string; order: Summary; setOrder: (o: Summary) => void }) {
  const [reporting, setReporting] = useState(false)
  const [type, setType] = useState('missing_item')
  const [narrative, setNarrative] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const s = order.shipment
  const sent = s && ['picked_up', 'delivered'].includes(s.status)
  const latest = order.claims.at(-1)

  const post = (label: string, path: string, init: RequestInit) => {
    setBusy(label)
    setError('')
    return loadPublic<Summary>(`/orders/${token}${path}`, { method: 'POST', ...init })
      .then(setOrder, (e) => setError(e.message))
      .finally(() => setBusy(''))
  }

  return (
    <>
      {s && (
        <p role="status">
          <strong>{SHIPMENT[s.status] ?? 'Preparing your delivery'}.</strong>
          {s.simulated && <mark> DEMO: simulated delivery</mark>}{' '}
          {s.share_link && (
            <a href={s.share_link} target="_blank" rel="noreferrer">
              Track your delivery
            </a>
          )}
        </p>
      )}
      {sent && !order.received && (
        <button disabled={!!busy} onClick={() => post('Saving…', '/received', {})}>
          I received my order
        </button>
      )}
      {order.received && <p>Thank you for confirming you received it.</p>}
      {order.claims.map((c) => (
        <p key={c.id}>
          <small>
            You reported “{c.type}” on {new Date(c.created_at).toLocaleString()}. Status: {c.status.replace(/_/g, ' ')}.
          </small>
        </p>
      ))}
      {latest && latest.status !== 'resolved' && latest.photos < 4 && (
        <label className="upload">
          Add a photo or your unboxing video to your report
          <input
            type="file"
            accept="image/*,video/mp4,video/quicktime,video/webm"
            disabled={!!busy}
            onChange={(e) => {
              const file = e.target.files?.[0]
              if (file) post('Uploading…', `/claims/${latest.id}/photos`, { headers: { 'Content-Type': file.type }, body: file })
            }}
          />
        </label>
      )}
      {reporting ? (
        <form
          onSubmit={(e) => {
            e.preventDefault()
            post('Sending…', '/claims', {
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ type, narrative }),
            }).then(() => setReporting(false))
          }}
        >
          <label>
            What happened?
            <select value={type} onChange={(e) => setType(e.target.value)}>
              {Object.entries(PROBLEMS).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
          </label>
          <label>
            Tell the seller more
            <textarea required rows={3} maxLength={2000} value={narrative} onChange={(e) => setNarrative(e.target.value)} />
          </label>
          <p>
            <small>You can add photos after sending.</small>
          </p>
          <button disabled={!!busy}>Send report</button>
        </form>
      ) : (
        <button onClick={() => setReporting(true)}>Report a problem</button>
      )}
      {error && <p role="alert">{error}</p>}
      {busy && <p role="status">{busy}</p>}
    </>
  )
}

/** Where PayMongo sends a customer who paid from a link in the chat. Coming back here proves nothing: only PayMongo's
 * webhook, or the seller rechecking with PayMongo, marks an order paid. */
export function Paid() {
  return (
    <main className="order">
      <h1>Thank you!</h1>
      <p>Your payment is being checked with PayMongo. The seller will confirm it in your chat. You can close this page.</p>
    </main>
  )
}

export default function Confirm({ token }: { token: string }) {
  const [order, setOrder] = useState<Summary>()
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  // Back from PayMongo: ask the backend to check with PayMongo. Being redirected here proves nothing by itself.
  const returned = new URLSearchParams(location.search).has('returned')

  useEffect(() => {
    const path = returned ? `/orders/${token}/check-payment` : `/orders/${token}`
    loadPublic<Summary>(path, { method: returned ? 'POST' : 'GET' }).then(setOrder, (e) => setError(e.message))
  }, [token, returned])

  const act = (path: string, then: (r: Summary & { checkout_url?: string }) => void) => {
    setBusy(true)
    setError('')
    loadPublic<Summary>(`/orders/${token}${path}`, { method: 'POST' })
      .then(then, (e) => setError(e.message))
      .finally(() => setBusy(false))
  }

  if (!order) return <main>{error ? <p role="alert">{error}</p> : 'Loading…'}</main>

  return (
    <main className="order">
      <h1>{order.status === 'confirmed' ? 'Your order' : 'Please check your order'}</h1>
      <table>
        <tbody>
          {order.items.map((i, n) => (
            <tr key={n}>
              <td>
                {i.quantity} × {i.name}
                {i.variant && ` (${i.variant})`}
              </td>
              <td>{peso(i.unit_price === null ? null : i.unit_price * i.quantity)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <th>Total</th>
            <th>{peso(order.total)}</th>
          </tr>
        </tfoot>
      </table>
      <dl>
        <dt>Deliver to</dt>
        <dd>{order.recipient_name}</dd>
        <dd>{order.phone}</dd>
        <dd>{order.address}</dd>
        {order.delivery_instructions && <dd>Note: {order.delivery_instructions}</dd>}
      </dl>
      {error && <p role="alert">{error}</p>}
      {order.status !== 'confirmed' ? (
        <>
          <p>If anything is wrong, message the seller and do not confirm.</p>
          <button onClick={() => act('/confirm', setOrder)} disabled={busy}>
            {busy ? 'Confirming…' : 'Yes, this is correct'}
          </button>
        </>
      ) : order.payment === 'verified' || order.gcash_confirmed ? (
        <>
          <p role="status">
            <strong>Payment received{order.payment !== 'verified' && ' by GCash'}. Thank you!</strong>
            {!order.shipment && ' The seller will prepare your order.'}
          </p>
          <AfterPayment token={token} order={order} setOrder={setOrder} />
        </>
      ) : order.payment === 'mismatched' ? (
        <p role="status">We received a payment that does not match this order. The seller will contact you.</p>
      ) : (
        <>
          <p role="status">
            {order.payment === 'pending' && returned
              ? 'We have not received your payment yet. If you just paid, wait a moment and check again.'
              : 'Order confirmed. Pay to complete it.'}
          </p>
          {order.test_mode && (
            <p className="claim" role="note">
              <mark>TEST</mark> <strong>This is a test. Do not scan the QR code</strong>: in test mode it is a real QR, and paying it with a
              real app sends real money. On the payment page, press <strong>Simulate Payment</strong>, then{' '}
              <strong>Authorize Test Payment</strong>.
            </p>
          )}
          <p className="actions">
            <button onClick={() => act('/pay', (r) => r.checkout_url && location.assign(r.checkout_url))} disabled={busy}>
              Pay {peso(order.total)} with QR Ph
            </button>
            {order.payment === 'pending' && (
              <button onClick={() => act('/check-payment', setOrder)} disabled={busy}>
                I have paid, check again
              </button>
            )}
          </p>
        </>
      )}
    </main>
  )
}
