import { useEffect, useRef, useState } from 'react'
import { load } from '../lib/firebase'
import { fromChat, peso } from '../lib/format'
import { parsePin, pinText } from '../lib/maps'
import type { ChatMessage, Order } from './OrderView'
import { Photo } from './Packing'
import PinMap from './PinMap'

/** A drop-off pin Google suggested from the order's address: how precise it is, and whether it is in the city the address names. */
type Suggestion = { lat: number; lng: number; formatted?: string; precision_text?: string; warning: string | null; not_found?: boolean }
/** What is at the chosen pin, checked against the order's address (Lalamove never compares them). */
type Where = { formatted: string; warning: string | null }

// A Google Maps link, or a pin as numbers, in a customer's message
const SHARED_PIN =
  /(https?:\/\/(maps\.app\.goo\.gl|goo\.gl\/maps|(www\.)?google\.[a-z.]+\/maps)\S*|-?\d{1,2}\.\d{3,}\s*,\s*-?\d{2,3}\.\d{3,})/

type Event = {
  id: string
  source: 'provider' | 'seller_reported' | 'customer_reported' | 'demo'
  raw_status: string
  status: string
  at: string
  note?: string | null
}
/** A courier's pickup or drop-off proof. image: the copy kept by the app, or a sample photo for the sandbox demo. */
export type Proof = {
  status: string | null
  at?: string | null
  delivered_at?: string | null // proofs kept before pickup photos existed
  source?: 'provider' | 'demo'
  image?: { path?: string; sample?: string } | null
  image_url?: string | null
  check?: { parcel_visible: boolean; outer_damage: string; seal_intact: boolean | null; label_match: boolean | null }
}
/** Who carried it, and where that came from: Lalamove, the sandbox demo, or the seller. */
export type Driver = { source: 'provider' | 'simulated' | 'seller_reported'; driver_id: string; name: string | null; plate: string | null }
export type Fulfillment = {
  id: string
  courier: 'lalamove' | 'simulator' | 'other'
  status: 'quoted' | 'booking' | 'booked' | 'driver_assigned' | 'picked_up' | 'delivered' | 'cancelled' | 'failed'
  quotation: {
    id: string
    service: string
    price: number
    expires_at: string
    livemode: boolean
    dropoff?: [number, number]
    distance_m?: number | null
  } | null
  provider_order_id: string | null
  share_link: string | null
  tracking: string | null
  driver_id: string | null
  driver?: Driver | null
  drivers?: { driver_id: string; source: string; at: string }[]
  pop?: Proof | null
  pod: Proof | null
  end_reason?: { who: string; why: string; note: string | null; by: string } | null
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
const DRIVER_SOURCE = { provider: 'from Lalamove', simulated: 'simulated', seller_reported: 'named by you' }
const WHO: Record<string, string> = {
  driver: 'The driver',
  customer: 'The customer',
  seller: 'Us',
  courier: 'The courier',
  unknown: 'Unknown',
}
const WHY: Record<string, string> = {
  customer_unreachable: 'Customer unreachable',
  wrong_pin: 'Wrong pin or address',
  refused: 'Refused at the door',
  driver_no_show: 'Driver did not come',
  damaged: 'Damaged before handover',
  other: 'Other',
}

/** The driver on a shipment, with where the details came from. */
export function DriverLine({ f }: { f: Fulfillment }) {
  const d = f.driver ?? (f.driver_id ? { source: 'provider' as const, driver_id: f.driver_id, name: null, plate: null } : null)
  if (!d) return null
  const changed = new Set((f.drivers ?? []).map((x) => x.driver_id)).size > 1
  return (
    <p>
      Driver: <strong>{d.name ?? `ID ${d.driver_id}`}</strong>
      {d.plate && <> · plate {d.plate}</>}{' '}
      <mark className={`source ${d.source === 'simulated' ? 'demo' : d.source}`}>{DRIVER_SOURCE[d.source]}</mark>
      {changed && <small> · the driver changed during this delivery</small>}
    </p>
  )
}

/** The courier's pickup and drop-off proofs, with their photos. They show the parcel's outside, never what was inside. */
export function Proofs({ orderId, f }: { orderId: string; f: Fulfillment }) {
  const shown = (['pop', 'pod'] as const).filter((k) => f[k])
  if (!shown.length) return null
  return (
    <div className="photos">
      {shown.map((k) => {
        const p = f[k]!
        const at = p.at ?? p.delivered_at
        const caption = `${k === 'pop' ? 'Pickup' : 'Drop-off'}: ${p.status ?? 'status unknown'}${at ? `, ${new Date(at).toLocaleString()}` : ''}${p.source === 'demo' ? ' · simulated' : ''}${p.check ? ` · outer damage: ${p.check.outer_damage}` : ''}`
        return p.image?.path || p.image?.sample ? (
          <Photo
            key={k}
            path={`/orders/${orderId}/shipments/${f.id}/proof/${k}`}
            alt={`${k === 'pop' ? 'Pickup' : 'Drop-off'} photo`}
            caption={caption}
          />
        ) : (
          <p key={k}>
            <small>
              {caption}
              {p.image_url && (
                <>
                  {' '}
                  ·{' '}
                  <a href={p.image_url} target="_blank" rel="noreferrer">
                    Lalamove's photo
                  </a>
                </>
              )}
            </small>
          </p>
        )
      })}
    </div>
  )
}

/** The seller names the rider, for another courier or a rider they arranged (a Favourite Driver, or one outside Lalamove). */
export function NameDriver({ path, run }: { path: string; run: (label: string, path: string, body?: object) => Promise<unknown> }) {
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [plate, setPlate] = useState('')
  if (!open) return <button onClick={() => setOpen(true)}>Name the rider</button>
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        run('Saving…', path, { name, plate }).then(() => setOpen(false))
      }}
    >
      <label>
        Rider's name
        <input required maxLength={100} value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label>
        Plate (optional)
        <input maxLength={20} value={plate} onChange={(e) => setPlate(e.target.value)} />
      </label>
      <button>Save rider</button> <small>Kept as named by you, for delivery patterns, such as a favourite or regular rider.</small>
    </form>
  )
}

/** Why a shipment failed or was cancelled: Lalamove never says, so the seller records it. */
function EndReason({ f, run }: { f: Fulfillment; run: (label: string, path: string, body?: object) => Promise<unknown> }) {
  const [who, setWho] = useState('unknown')
  const [why, setWhy] = useState('customer_unreachable')
  const [note, setNote] = useState('')
  if (f.end_reason)
    return (
      <p>
        <small>
          Why: {WHO[f.end_reason.who]}, {WHY[f.end_reason.why].toLowerCase()}
          {f.end_reason.note && <> ({f.end_reason.note})</>}. Recorded by {f.end_reason.by}.
        </small>
      </p>
    )
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        run('Saving…', `/${f.id}/end-reason`, { who, why, note })
      }}
    >
      <label>
        Who
        <select value={who} onChange={(e) => setWho(e.target.value)}>
          {Object.entries(WHO).map(([k, v]) => (
            <option key={k} value={k}>
              {v}
            </option>
          ))}
        </select>
      </label>
      <label>
        Why
        <select value={why} onChange={(e) => setWhy(e.target.value)}>
          {Object.entries(WHY).map(([k, v]) => (
            <option key={k} value={k}>
              {v}
            </option>
          ))}
        </select>
      </label>
      <label>
        Note{why === 'other' ? '' : ' (optional)'}
        <input required={why === 'other'} maxLength={500} value={note} onChange={(e) => setNote(e.target.value)} />
      </label>
      <button>Record why</button>
    </form>
  )
}

const NEXT: Record<string, string[]> = {
  simulator: ['driver_assigned', 'picked_up', 'delivered'],
  other: ['picked_up', 'delivered', 'failed'],
}

/** Dispatch is only offered once this version is confirmed, paid and packed. Booking needs the seller to approve a quote. */
export default function Dispatch({
  order,
  onChange,
  onSent,
  chat,
}: {
  order: Order
  onChange: (o: Order) => void
  onSent?: () => void
  chat?: ChatMessage[]
}) {
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const f = order.fulfillment
  const quoted = f?.quotation?.dropoff
  const [pin, setPin] = useState(quoted ? pinText(quoted[0], quoted[1]) : '')
  const [pinFrom, setPinFrom] = useState(quoted ? 'the last quote' : '')
  const [suggestion, setSuggestion] = useState<Suggestion | null>()
  const [where, setWhere] = useState<Where | null>()
  const [service, setService] = useState('MOTORCYCLE')
  const [tracking, setTracking] = useState('')
  const [openedAt] = useState(() => Date.now())
  // A failed or cancelled shipment stays on record; the seller can then quote again or send it another way.
  const ended = !!f && ['cancelled', 'failed'].includes(f.status)
  const [outcome, setOutcome] = useState('delivered')
  const choosing = order.status === 'confirmed' && (!f || f.status === 'quoted' || ended) && !(!f && order.dispatch_blocked)

  // Suggest a pin from the order's address once the panel can quote. Kept on the order, so this is one lookup per address.
  useEffect(() => {
    if (!choosing) return
    load<Suggestion | null>(`/orders/${order.id}/dispatch/suggest`).then(
      (s) => {
        setSuggestion(s)
        if (!s || s.not_found) return // not found: the map opens around the pickup, and the seller clicks the spot
        setPin((p) => p || pinText(s.lat, s.lng))
        setPinFrom((w) => w || "the order's address")
      },
      () => setSuggestion(null),
    )
  }, [order.id, choosing])

  // A Lalamove sandbox booking moves on by itself, one simulated step a second (the backend applies them when the order
  // loads), so reload it until it is delivered.
  const sandboxMoving =
    f?.courier === 'lalamove' && f.quotation?.livemode === false && ['booked', 'driver_assigned', 'picked_up'].includes(f.status)
  const changed = useRef(onChange)
  changed.current = onChange
  useEffect(() => {
    if (!sandboxMoving) return
    const timer = setInterval(
      () =>
        load<Order>(`/orders/${order.id}`).then(
          (o) => changed.current(o),
          () => {},
        ),
      1000,
    )
    return () => clearInterval(timer)
  }, [sandboxMoving, order.id])

  // What is at the pin, checked against the address, once the pin has stopped moving for a moment.
  const pinned = choosing ? parsePin(pin) : null
  useEffect(() => {
    if (!pinned) return setWhere(undefined)
    const timer = setTimeout(
      () =>
        load<Where | null>(`/orders/${order.id}/dispatch/where`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(pinned),
        }).then(setWhere, () => setWhere(null)),
      700,
    )
    return () => clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [order.id, pinned?.lat, pinned?.lng])

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

  // The pin in a pasted link or the customer's message, read by the backend (it follows short links, only to Google).
  const readPin = (text: string, from: string) => {
    setBusy('Reading the pin…')
    setError('')
    return load<{ lat: number; lng: number }>(`/orders/${order.id}/dispatch/pin`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    })
      .then(
        (p) => (setPin(pinText(p.lat, p.lng)), setPinFrom(from)),
        (e) => setError(e.message),
      )
      .finally(() => setBusy(''))
  }
  const shared = [...(chat ?? [])].reverse().find((m) => !m.from_page && SHARED_PIN.test(m.text))
  const here = parsePin(pin)
  const center = here ?? (suggestion?.not_found ? suggestion : null)

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
      {f?.courier === 'lalamove' && f.quotation?.livemode === false && f.status !== 'quoted' && f.status !== 'booking' && (
        <p>
          <small>
            Lalamove sandbox: the quote and booking are real sandbox calls, but the sandbox rarely assigns a driver, so the delivery steps,
            the driver and the pickup and drop-off photos are simulated for the demo, one step a second, and labelled Simulated.
          </small>
        </p>
      )}

      {ended && (
        <div className="claim">
          <p>
            <small>
              This delivery {f.status === 'failed' ? 'failed' : 'was cancelled'}; it stays on record below. Get a new quote, or send it
              another way.
            </small>
          </p>
          <EndReason f={f} run={run} />
        </div>
      )}
      {(!f || f.status === 'quoted' || ended) && (
        <>
          <form
            onSubmit={(e) => {
              e.preventDefault()
              run('Getting a Lalamove quote…', '/quote', { pin, service })
            }}
          >
            <p>
              Drop-off for <strong>{order.draft.address.value}</strong>
            </p>
            {suggestion?.not_found && !here && (
              <p className="claim">
                <small>
                  <strong>{suggestion.warning}</strong>
                </small>
              </p>
            )}
            {suggestion && !suggestion.not_found && pinFrom === "the order's address" && (
              <p className={suggestion.warning ? 'claim' : undefined}>
                <small>
                  Google Maps found: {suggestion.formatted} ({suggestion.precision_text}).{' '}
                  {suggestion.warning && <strong>{suggestion.warning}</strong>}
                </small>
              </p>
            )}
            {center && (
              <PinMap
                lat={center.lat}
                lng={center.lng}
                placed={!!here}
                label="Drop-off pin: drag it, or click the map"
                onMove={(lat, lng) => (setPin(pinText(lat, lng)), setPinFrom('the map'))}
              />
            )}
            {here && where && (
              <p className={where.warning ? 'claim' : undefined}>
                <small>
                  The rider will be sent to this pin: {where.formatted}. Lalamove does not check it against the address.{' '}
                  {where.warning && <strong>{where.warning}</strong>}
                </small>
              </p>
            )}
            {shared && pinFrom !== 'the customer' && (
              <p>
                <small>The customer sent a pin in the chat ({new Date(shared.created_time).toLocaleString()}).</small>{' '}
                <button type="button" disabled={!!busy} onClick={() => readPin(shared.text, 'the customer')}>
                  Use the customer's pin
                </button>
              </p>
            )}
            <label>
              Pin{pinFrom && <small> (from {pinFrom})</small>}: or paste numbers or a Google Maps link, such as one the customer sent
              <input
                required
                placeholder="14.6091, 121.0223"
                value={pin}
                onChange={(e) => (setPin(e.target.value), setPinFrom(''))}
                onBlur={(e) => !parsePin(e.target.value) && e.target.value.trim() && readPin(e.target.value, 'the pasted link')}
              />
            </label>
            {fromChat(order) && (
              <p>
                <button type="button" disabled={!!busy} onClick={() => run('Sending…', '/ask-pin').then(onSent)}>
                  Ask the customer for their map pin
                </button>
              </p>
            )}
            <label>
              Vehicle
              <select value={service} onChange={(e) => setService(e.target.value)}>
                <option value="MOTORCYCLE">Motorcycle</option>
                <option value="SEDAN">Sedan</option>
                <option value="MPV">MPV</option>
                <option value="VAN">Van</option>
              </select>
            </label>
            <button disabled={!!busy}>{f?.status === 'quoted' ? 'Get a new quote' : 'Get Lalamove quote'}</button>
          </form>
          {f?.quotation && f.status === 'quoted' && (
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
              {f.quotation.livemode === false && (
                <label>
                  Sandbox only: how the simulated delivery ends
                  <select value={outcome} onChange={(e) => setOutcome(e.target.value)}>
                    <option value="delivered">Delivered</option>
                    <option value="damaged">Delivered with a crushed corner (damaged in transit)</option>
                    <option value="failed">Failed delivery (the rider could not hand it over)</option>
                    <option value="cancelled">Cancelled by the driver before pickup</option>
                  </select>
                </label>
              )}
              <button
                className="primary"
                disabled={!!busy || !!expired}
                onClick={() => run('Booking with Lalamove…', '/book', { sandbox_outcome: outcome })}
              >
                Approve {peso(f.quotation.price)} and book
              </button>
            </p>
          )}
          {(!f || ended) && (
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
          </p>
          <DriverLine f={f} />
          {!f.driver?.name &&
            f.quotation?.livemode !== false &&
            ['driver_assigned', 'picked_up', 'delivered', 'booked'].includes(f.status) && <NameDriver path={`/${f.id}/driver`} run={run} />}
          <ol className="timeline">
            {f.events.map((e) => (
              <li key={e.id}>
                {STATUS[e.status] ?? e.status} <small>({e.raw_status})</small> ·{' '}
                <mark className={`source ${e.source}`}>{SOURCE[e.source]}</mark> <small>{new Date(e.at).toLocaleString()}</small>
                {e.note && <small> · {e.note}</small>}
              </li>
            ))}
          </ol>
          <Proofs orderId={order.id} f={f} />
          {(f.pop || f.pod) && (
            <p>
              <small>Pickup and drop-off photos show the parcel's outside when it was handed over, not what was inside.</small>
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
      {!!order.fulfillment_history?.length && (
        <details>
          <summary>Earlier shipments ({order.fulfillment_history.length})</summary>
          {[...order.fulfillment_history].reverse().map((h) => (
            <div key={h.id} className="claim">
              <p>
                {h.courier === 'lalamove' ? `Lalamove ${h.provider_order_id ?? ''}` : h.courier === 'other' ? h.tracking : 'Simulator'} ·{' '}
                {STATUS[h.status] ?? h.status}
              </p>
              <DriverLine f={h} />
              <Proofs orderId={order.id} f={h} />
              {['cancelled', 'failed'].includes(h.status) && <EndReason f={h} run={run} />}
            </div>
          ))}
        </details>
      )}
      {error && <p role="alert">{error}</p>}
      {busy && <p role="status">{busy}</p>}
    </section>
  )
}
