import { useState } from 'react'
import { load } from '../lib/firebase'
import { fromChat, peso } from '../lib/format'
import type { Order } from './OrderView'

type Event = {
  id: string
  source: 'provider' | 'seller_reported' | 'customer_reported' | 'demo'
  raw_status: string
  status: string
  at: string
  note?: string | null
}
export type Fulfillment = {
  id: string
  courier: 'lalamove' | 'simulator' | 'other'
  status: 'quoted' | 'booking' | 'booked' | 'driver_assigned' | 'picked_up' | 'delivered' | 'cancelled' | 'failed'
  quotation: { id: string; service: string; price: number; expires_at: string; livemode: boolean } | null
  provider_order_id: string | null
  share_link: string | null
  tracking: string | null
  driver_id: string | null
  pod: { status: string | null; image_url: string | null; delivered_at: string | null } | null
  events: Event[]
  booking_uncertain?: boolean
  problem?: string
  replaces: string | null
  claim_id: string | null
}

const SOURCE = { provider: 'Lalamove', seller_reported: 'Seller-reported', customer_reported: 'Customer-reported', demo: 'Simulated' }
const STATUS: Record<string, string> = {
  quoted: 'Quote ready',
  booking: 'Booking…',
  booked: 'Booked, finding a driver',
  driver_assigned: 'Driver assigned (not picked up yet)',
  picked_up: 'Picked up',
  delivered: 'Delivered (by the courier’s account)',
  cancelled: 'Cancelled',
  failed: 'Failed',
}
const NEXT: Record<string, string[]> = {
  simulator: ['driver_assigned', 'picked_up', 'delivered'],
  other: ['picked_up', 'delivered', 'failed'],
}

/** Dispatch is only offered once this version is confirmed, paid and packed. Booking needs the seller to approve a quote. */
export default function Dispatch({ order, onChange, onSent }: { order: Order; onChange: (o: Order) => void; onSent?: () => void }) {
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [pin, setPin] = useState('')
  const [service, setService] = useState('MOTORCYCLE')
  const [tracking, setTracking] = useState('')
  const [openedAt] = useState(() => Date.now())
  const f = order.fulfillment

  if (order.status !== 'confirmed') return null
  if (!f && order.dispatch_blocked)
    return order.packing?.completed || order.replacement ? <p className="packing">Dispatch: {order.dispatch_blocked}.</p> : null

  const run = (label: string, path: string, body?: object) => {
    setBusy(label)
    setError('')
    return load<Order>(path.startsWith('/ask') ? `/orders/${order.id}${path}` : `/orders/${order.id}/dispatch${path}`, {
      method: 'POST',
      ...(body && { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
    })
      .then(onChange, (e) => setError(e.message))
      .finally(() => setBusy(''))
  }

  const expired = f?.quotation && Date.parse(f.quotation.expires_at) < openedAt // the backend re-checks when booking
  const live = f && !['cancelled', 'failed'].includes(f.status)
  // Asked about this shipment, not an earlier one that was replaced
  const askedReceived = f && order.asked?.received && Date.parse(order.asked.received.at) >= Date.parse(f.events[0]?.at ?? '')

  return (
    <section className="packing">
      <h3>
        Delivery{f && ` · ${STATUS[f.status]}`}
        {f?.courier === 'simulator' && <mark> SIMULATED</mark>}
        {f?.courier === 'lalamove' && f.quotation && !f.quotation.livemode && <mark> LALAMOVE SANDBOX</mark>}
      </h3>
      {order.replacement && <p>Replacement for a customer claim. The original shipment stays on record.</p>}

      {(!f || f.status === 'quoted') && (
        <>
          <form
            onSubmit={(e) => {
              e.preventDefault()
              run('Getting a Lalamove quote…', '/quote', { pin, service })
            }}
          >
            <label>
              Drop-off pin for {order.draft.address.value}: paste it from Google Maps, or the pin the customer sent
              <input required placeholder="14.6091, 121.0223" value={pin} onChange={(e) => setPin(e.target.value)} />
            </label>
            <label>
              Vehicle
              <select value={service} onChange={(e) => setService(e.target.value)}>
                <option value="MOTORCYCLE">Motorcycle</option>
                <option value="SEDAN">Sedan</option>
                <option value="MPV">MPV</option>
                <option value="VAN">Van</option>
              </select>
            </label>
            <button disabled={!!busy}>{f ? 'Get a new quote' : 'Get Lalamove quote'}</button>
          </form>
          {f?.quotation && (
            <p>
              Lalamove {f.quotation.service.toLowerCase()}: <strong>{peso(f.quotation.price)}</strong>, paid by you.{' '}
              {expired ? (
                <em>The quote has expired; get a new one.</em>
              ) : (
                <>Valid until {new Date(f.quotation.expires_at).toLocaleTimeString()}.</>
              )}
              {f.problem && (
                <>
                  <br />
                  <span role="alert">Last booking attempt failed: {f.problem}</span>
                </>
              )}
              <br />
              <button className="primary" disabled={!!busy || !!expired} onClick={() => run('Booking with Lalamove…', '/book')}>
                Approve {peso(f.quotation.price)} and book
              </button>
            </p>
          )}
          {!f && (
            <details>
              <summary>Other ways to send it</summary>
              <p className="actions">
                <button disabled={!!busy} onClick={() => run('Starting…', '/start', { courier: 'simulator' })}>
                  Use the delivery simulator (labelled as simulated)
                </button>
              </p>
              <form
                onSubmit={(e) => {
                  e.preventDefault()
                  run('Saving…', '/start', { courier: 'other', tracking })
                }}
              >
                <label>
                  Another courier: name and tracking number. You report its updates yourself.
                  <input required placeholder="J&T 123456789" value={tracking} onChange={(e) => setTracking(e.target.value)} />
                </label>
                <button disabled={!!busy}>Record shipment</button>
              </form>
            </details>
          )}
        </>
      )}

      {f?.status === 'booking' && (
        <p role="status">
          {f.booking_uncertain
            ? 'Lalamove did not answer. If it created the order, its update will link it here. Do not book again while the quote is valid.'
            : 'Booking with Lalamove…'}{' '}
          {f.booking_uncertain && expired && (
            <button disabled={!!busy} onClick={() => run('Releasing…', '/release')}>
              No order appeared: release the quote
            </button>
          )}
        </p>
      )}

      {f && f.status !== 'quoted' && f.status !== 'booking' && (
        <>
          <p>
            {f.courier === 'lalamove' && <>Lalamove order {f.provider_order_id}. </>}
            {f.tracking && <>{f.tracking}. </>}
            {f.share_link && (
              <a href={f.share_link} target="_blank" rel="noreferrer">
                Tracking link
              </a>
            )}
            {f.driver_id && <> · Driver ID {f.driver_id}</>}
          </p>
          <ol className="timeline">
            {f.events.map((e) => (
              <li key={e.id}>
                {STATUS[e.status] ?? e.status} <small>({e.raw_status})</small> ·{' '}
                <mark className={`source ${e.source}`}>{SOURCE[e.source]}</mark> <small>{new Date(e.at).toLocaleString()}</small>
                {e.note && <small> · {e.note}</small>}
              </li>
            ))}
          </ol>
          {f.pod && (
            <p>
              Proof of delivery from Lalamove: {f.pod.status ?? 'status unknown'}
              {f.pod.delivered_at && <>, {new Date(f.pod.delivered_at).toLocaleString()}</>}
              {f.pod.image_url && (
                <>
                  {' '}
                  ·{' '}
                  <a href={f.pod.image_url} target="_blank" rel="noreferrer">
                    image
                  </a>
                </>
              )}
              . It shows the parcel was handed over, not what was inside.
            </p>
          )}
          <p>
            {order.receipt?.fulfillment_id === f.id ? (
              <strong>
                The customer confirmed they received it{order.receipt.source === 'customer_chat' && ' by tapping Received in the chat'} (
                {new Date(order.receipt.at).toLocaleString()}).
              </strong>
            ) : f.status === 'delivered' ? (
              <em>Delivered, but the customer has not confirmed receipt.</em>
            ) : null}
          </p>
          {fromChat(order) && order.receipt?.fulfillment_id !== f.id && ['picked_up', 'delivered'].includes(f.status) && (
            <p>
              {askedReceived && (
                <>
                  Asked in the chat. Waiting for the customer to tap <strong>{order.asked!.received!.text}</strong>.{' '}
                </>
              )}
              <button disabled={!!busy} onClick={() => run('Sending…', '/ask-received').then(onSent)}>
                {askedReceived ? 'Ask again in chat' : 'Ask the customer to confirm receipt in chat'}
              </button>
            </p>
          )}
          {live && (
            <p className="actions">
              {f.courier === 'lalamove' && (
                <button disabled={!!busy} onClick={() => run('Asking Lalamove…', '/refresh')}>
                  Refresh status from Lalamove
                </button>
              )}
              {NEXT[f.courier]
                ?.filter((s) => !f.events.some((e) => e.status === s))
                .map((s) => (
                  <button key={s} disabled={!!busy} onClick={() => run('Saving…', '/step', { status: s })}>
                    {f.courier === 'simulator' ? 'Simulate' : 'Mark'}: {STATUS[s].split(' (')[0].toLowerCase()}
                  </button>
                ))}
              {!['picked_up', 'delivered'].includes(f.status) && (
                <button disabled={!!busy} onClick={() => confirm('Cancel this shipment?') && run('Cancelling…', '/cancel', { reason: '' })}>
                  Cancel shipment
                </button>
              )}
            </p>
          )}
        </>
      )}
      {error && <p role="alert">{error}</p>}
      {busy && <p role="status">{busy}</p>}
    </section>
  )
}
