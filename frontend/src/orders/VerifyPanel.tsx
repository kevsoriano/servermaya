import { useState } from 'react'
import { load } from '../lib/firebase'
import { CLAIM_TYPES, fromChat, peso, STATUS } from '../lib/format'
import Reply from '../conversations/Reply'
import GcashPayment from './GcashPayment'
import type { ChatMessage, Order } from './OrderView'

type Gate = { label: string; state: 'ok' | 'warn' | 'todo' | 'off'; text: string }

const ICON = { ok: '✓', warn: '⚠', todo: '–', off: '·' }
const CAUSE: Record<string, string> = {
  courier_transit: 'courier, in transit',
  seller_error: 'our error',
  customer: 'the customer',
  unknown: 'unknown',
}

/** Paid for this version: PayMongo verified it, or the seller found the GCash payment in their own history. */
const paidBy = (o: Order) =>
  o.payment?.status === 'verified' && o.payment.version === o.version
    ? 'paymongo'
    : o.manual_payment?.status === 'seller_confirmed' && o.manual_payment.version === o.version
      ? 'gcash'
      : null

/** One line per guardrail: what is verified, what is only claimed, and what blocks the next step. */
function gates(o: Order): Gate[] {
  const confirmation = o.status === 'confirmed' ? o.confirmations.at(-1) : undefined
  const paid = paidBy(o)
  const said = o.draft.claims.some((c) => c.kind === 'payment')
  const f = o.fulfillment
  const open = (o.claims ?? []).filter((c) => c.status !== 'resolved')
  return [
    o.status === 'cancelled'
      ? { label: 'Order', state: 'off', text: 'Cancelled' }
      : confirmation?.kind === 'customer'
        ? { label: 'Confirmed', state: 'ok', text: `By the customer, version ${confirmation.version}` }
        : confirmation
          ? { label: 'Confirmed', state: 'warn', text: 'Only by you, not by the customer' }
          : {
              label: 'Confirmed',
              state: 'todo',
              text:
                o.status === 'awaiting_confirmation' ? `Waiting for the customer to tap ${o.asked?.confirm?.text ?? 'Confirm'}` : 'Not yet',
            },
    paid === 'paymongo'
      ? { label: 'Payment', state: 'ok', text: `Verified by PayMongo, ${peso(o.payment!.paid_amount ?? o.payment!.amount)}` }
      : paid === 'gcash'
        ? { label: 'Payment', state: 'ok', text: 'GCash found in your own history (not verified by PayMongo)' }
        : o.payment?.status === 'mismatched'
          ? { label: 'Payment', state: 'warn', text: `Needs review: ${o.payment.problem}` }
          : said || o.manual_payment?.claims.length
            ? { label: 'Payment', state: 'warn', text: 'The customer says they paid: not verified' }
            : {
                label: 'Payment',
                state: 'todo',
                text: o.payment || o.manual_payment ? 'Requested, nothing received yet' : 'Not requested',
              },
    o.packing?.completed
      ? { label: 'Packing', state: 'ok', text: `Packed and checked${o.packing.weight_g ? `, ${o.packing.weight_g} g` : ''}` }
      : o.packing?.photos.length
        ? { label: 'Packing', state: 'todo', text: `${o.packing.photos.length} photo(s), not marked packed` }
        : { label: 'Packing', state: o.packing_blocked ? 'off' : 'todo', text: o.packing_blocked ? 'Waits for payment' : 'Not started' },
    f
      ? {
          label: 'Delivery',
          state: f.status === 'failed' || f.status === 'cancelled' ? 'warn' : f.status === 'delivered' ? 'ok' : 'todo',
          text: `${STATUS_SHIP[f.status] ?? f.status}${f.driver?.name ? ` · ${f.driver.name}` : ''}${o.receipt?.fulfillment_id === f.id ? ' · received by the customer' : ''}`,
        }
      : {
          label: 'Delivery',
          state: o.dispatch_blocked ? 'off' : 'todo',
          text: o.dispatch_blocked ? 'Waits for payment and packing' : 'Not booked',
        },
    ...(open.length
      ? [
          {
            label: 'Claims',
            state: 'warn' as const,
            text: open
              .map(
                (c) =>
                  `${CLAIM_TYPES[c.type]}${c.custody?.first_break ? `, first problem at ${c.custody.steps.find((s) => s.step === c.custody!.first_break)?.label.toLowerCase()}` : ''}, suggests ${CAUSE[c.custody?.suggested_cause ?? 'unknown']}`,
              )
              .join('; '),
          },
        ]
      : []),
  ]
}

const STATUS_SHIP: Record<string, string> = {
  quoted: 'Quote ready',
  booking: 'Booking…',
  booked: 'Booked, finding a driver',
  driver_assigned: 'Driver assigned',
  picked_up: 'Picked up',
  delivered: 'Delivered',
  cancelled: 'Cancelled',
  failed: 'Failed',
}

/** The order beside its chat, guardrails first: one line per check that gates shipping or refunding, and the one action due
 * now. Editing, packing photos, delivery booking and claims are worked on in the Orders tab (onOpen). */
export default function VerifyPanel({
  order,
  onChange,
  onSent,
  onOpen,
  chat,
}: {
  order: Order
  onChange: (o: Order) => void
  onSent?: () => void
  onOpen: (id: string) => void
  chat?: ChatMessage[]
}) {
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const o = order
  const call = (label: string, path: string) => {
    setBusy(label)
    setError('')
    return load<Order>(`/orders/${o.id}${path}`, { method: 'POST' })
      .then((x) => (onChange(x), onSent?.()))
      .catch((e) => setError(e.message))
      .finally(() => setBusy(''))
  }
  const open = () => onOpen(o.id)
  const items = o.draft.items.map((i) => `${i.quantity}× ${i.name}${i.variant ? ` (${i.variant})` : ''}`).join(', ') || 'No items yet'
  const f = o.fulfillment
  const live = f && !['cancelled', 'failed'].includes(f.status)
  const paid = paidBy(o)
  const openClaims = (o.claims ?? []).filter((c) => c.status !== 'resolved')
  const received = o.receipt?.fulfillment_id && o.receipt.fulfillment_id === f?.id
  const inChat = fromChat(o)
  // The customer wrote since it was sent (not a tap on Confirm or Received) in messages no claim holds yet: maybe a problem.
  const taps = new Set([o.asked?.confirm?.text, o.asked?.received?.text])
  const sentAt = Date.parse([f, ...(o.fulfillment_history ?? [])].find(Boolean)?.events[0]?.at ?? '')
  const inClaims = new Set((o.claims ?? []).flatMap((c) => c.message_ids ?? []))
  const wroteSince = !!chat?.some(
    (m) => !m.from_page && Date.parse(m.created_time) > sentAt && !taps.has(m.text.trim()) && !inClaims.has(m.id),
  )

  // The one thing due now. Anything that sends a message stays here, beside the chat; the rest opens the order.
  let next: React.ReactNode = null
  if (o.status === 'cancelled') next = null
  else if (openClaims.length)
    next = (
      <button className="primary" onClick={open}>
        Review the claim →
      </button>
    )
  else if (wroteSince && received)
    // After they confirmed receipt, new messages are as likely a new order as a problem: offer both, push neither.
    next = (
      <>
        <p>
          The customer wrote again since receiving this order. A new order? Use <strong>+ New order</strong> below. A problem with this one?
        </p>
        <button onClick={open}>Open a claim →</button>
      </>
    )
  else if (wroteSince)
    next = (
      <>
        <p>The customer wrote in the chat since the order was sent. If it is about a problem, open a claim from those messages.</p>
        <button className="primary" onClick={open}>
          Open a claim →
        </button>
      </>
    )
  else if (o.status === 'draft')
    next = o.missing.length ? (
      <>
        <p>Fill in {o.missing.join(', ')} before asking the customer to confirm.</p>
        <button onClick={open}>Edit the order →</button>
      </>
    ) : (
      inChat && (
        <button className="primary" disabled={!!busy} onClick={() => call('Sending…', '/ask-confirmation')}>
          Send for confirmation in chat
        </button>
      )
    )
  else if (o.status === 'awaiting_confirmation')
    next = inChat && (
      <button disabled={!!busy} onClick={() => call('Sending…', '/ask-confirmation')}>
        Send the confirmation again
      </button>
    )
  else if (!paid)
    next = (
      <>
        {!o.payment && !o.manual_payment && <p>How will the customer pay?</p>}
        <div className="choices">
          {inChat && (!o.payment || o.payment.status === 'expired') && (
            <button disabled={!!busy} onClick={() => call('Sending…', '/payment-link')}>
              Send PayMongo payment link
            </button>
          )}
          <GcashPayment order={o} onChange={onChange} write={setMessage} onSent={onSent} bare />
        </div>
        {o.payment?.status === 'pending' && <p>Waiting for the PayMongo payment of {peso(o.payment.amount)}.</p>}
      </>
    )
  else if (!o.packing?.completed)
    next = (
      <button className="primary" onClick={open}>
        Pack it: photos and check →
      </button>
    )
  else if (!live && !received)
    next = (
      <button className="primary" onClick={open}>
        Book the delivery →
      </button>
    )
  else if (live && f && ['picked_up', 'delivered'].includes(f.status) && !received && inChat)
    next = (
      <button className="primary" disabled={!!busy} onClick={() => call('Sending…', '/ask-received')}>
        {o.asked?.received ? 'Ask again to confirm receipt' : 'Ask the customer to confirm receipt in chat'}
      </button>
    )

  return (
    <section className="verify">
      <div className="verify-head">
        <div>
          <strong>Order #{o.id.slice(0, 5).toUpperCase()}</strong> · {STATUS[o.status]}
          <br />
          <small>
            {items} · {o.draft.total === null ? 'no total' : peso(o.draft.total)}
          </small>
        </div>
        <button onClick={open}>Open order →</button>
      </div>
      <ul className="gates">
        {gates(o).map((g) => (
          <li key={g.label} className={g.state}>
            <span aria-hidden>{ICON[g.state]}</span>
            <strong>{g.label}</strong> <span>{g.text}</span>
          </li>
        ))}
      </ul>
      {next && (
        <div className="next">
          <h4>Next step</h4>
          {next}
        </div>
      )}
      {inChat && message && (
        <Reply conversationId={o.source.conversation_id} orderId={o.id} text={message} setText={setMessage} onSent={onSent} />
      )}
      {error && <p role="alert">{error}</p>}
      {busy && <p role="status">{busy}</p>}
    </section>
  )
}
