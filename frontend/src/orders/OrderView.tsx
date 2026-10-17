import { useEffect, useState } from 'react'
import { load } from '../lib/firebase'
import { fromChat, peso, STATUS } from '../lib/format'
import Reply from '../conversations/Reply'
import Signals from '../conversations/Signals'
import Packing, { type PackingState } from './Packing'
import GcashPayment, { type ManualPayment } from './GcashPayment'
import Dispatch, { type Fulfillment } from './Dispatch'
import Claims, { type Claim, type Refund } from './Claims'

/** from_order: not in this chat, copied from the customer's earlier confirmed order with that id. */
type Sourced = { value: string | null; sources: string[]; from_order?: string }
type Item = {
  name: string
  variant: string | null
  quantity: number
  unit_price: number | null
  sources: string[]
  /** Gemini read a price, but no seller message writes it, so it was left blank. */
  price_withheld?: boolean
  /** chat: a seller message states it. catalog: no message does, so it is the seller's catalog price. */
  price_from?: 'chat' | 'catalog' | null
}
export type Order = {
  id: string
  status: 'draft' | 'awaiting_confirmation' | 'confirmed' | 'cancelled'
  version: number
  source: { type: string; conversation_id: string }
  created_at: string
  confirm_token: string | null
  confirmations: { version: number; kind: 'customer' | 'seller_asserted'; by: string; at: string; message_id?: string }[]
  /** What the seller asked in the chat, with the quick-reply button's text the customer taps to answer. */
  asked?: { confirm?: { text: string; version: number; at: string }; received?: { text: string; at: string } }
  missing: string[]
  cancelled?: { by: string; at: string; reason: string | null }
  payment?: {
    status: 'pending' | 'verified' | 'mismatched' | 'expired'
    amount: number
    version: number
    livemode: boolean
    payment_id?: string
    paid_amount?: number
    checked_at?: string
    problem?: string
  } | null
  payment_review?: { payment_id: string; paid_amount: number; reason: string }[]
  manual_payment?: ManualPayment | null
  manual_payment_history?: ManualPayment[]
  packing: PackingState | null
  packing_blocked: string | null
  dispatch_blocked: string | null
  /** From the seller's catalog, checked in code: a chat price that differs from it, and stock that is short. */
  catalog_notes?: string[]
  fulfillment?: Fulfillment | null
  fulfillment_history?: Fulfillment[]
  receipt?: { status: 'customer_confirmed'; fulfillment_id: string; source: string; at: string } | null
  replacement?: { claim_id: string; action: string; fulfillment_id: string | null } | null
  claims?: Claim[]
  refunds?: Refund[]
  refundable: number
  messages: { id: string; text: string; from_page: boolean; created_time: string; from_name?: string }[]
  draft: {
    items: Item[]
    total: number | null
    recipient_name: Sourced
    phone: Sourced
    address: Sourced
    delivery_instructions: Sourced
    claims: { kind: string; text: string; reference: string | null; pressure: boolean; sources: string[] }[]
    clarification_questions: string[]
  }
}

const FIELDS = [
  ['recipient_name', 'Recipient'],
  ['phone', 'Phone'],
  ['address', 'Address'],
  ['delivery_instructions', 'Delivery instructions'],
] as const

type Form = { items: { name: string; variant: string; quantity: string; price: string }[] } & Record<(typeof FIELDS)[number][0], string>

const toForm = (d: Order['draft']): Form => ({
  items: d.items.map((i) => ({
    name: i.name,
    variant: i.variant ?? '',
    quantity: String(i.quantity),
    price: i.unit_price === null ? '' : String(i.unit_price / 100),
  })),
  recipient_name: d.recipient_name.value ?? '',
  phone: d.phone.value ?? '',
  address: d.address.value ?? '',
  delivery_instructions: d.delivery_instructions.value ?? '',
})

export type ChatMessage = { id: string; text: string; from_page: boolean; created_time: string; attachments: { kind: string }[] }

/** checks: false inside a chat, which shows the chat's checks itself with every message, not just this order's snapshot.
 * chat: the live thread, when shown inside it. onSent: after a message went to the chat. */
export default function OrderView({
  order,
  onChange,
  checks = true,
  chat,
  onSent,
}: {
  order: Order
  onChange: (o: Order) => void
  checks?: boolean
  chat?: ChatMessage[]
  onSent?: () => void
}) {
  const [form, setForm] = useState<Form>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState<'link' | 'details'>()
  const [message, setMessage] = useState('')

  // The customer confirms and pays on their own page, usually in another tab, so reload the order on coming back.
  useEffect(() => {
    const onFocus = () => load<Order>(`/orders/${order.id}`).then(onChange, () => {})
    window.addEventListener('focus', onFocus)
    return () => window.removeEventListener('focus', onFocus)
  }, [order.id, onChange])

  const { draft, messages } = order
  const confirmation = order.status === 'confirmed' ? order.confirmations.at(-1) : undefined
  const earlier = order.confirmations.filter((c) => c !== confirmation)
  const link = order.confirm_token && `${location.origin}/c/${order.confirm_token}`
  const { payment } = order
  const paymentClaim = draft.claims.find((c) => c.kind === 'payment')

  const details = [
    ...draft.items.map(
      (i) =>
        `${i.quantity} × ${i.name}${i.variant ? ` (${i.variant})` : ''} - ${peso(i.unit_price === null ? null : i.unit_price * i.quantity)}`,
    ),
    `Total: ${draft.total === null ? 'to be confirmed' : peso(draft.total)}`,
    '',
    `Deliver to: ${[draft.recipient_name.value, draft.phone.value, draft.address.value].filter(Boolean).join(', ')}`,
    ...(draft.delivery_instructions.value ? [`Note: ${draft.delivery_instructions.value}`] : []),
  ].join('\n')

  const call = (path: string, init: RequestInit, sends = false) => {
    setBusy(true)
    setError('')
    return load<Order>(`/orders/${order.id}${path}`, init)
      .then((o) => {
        onChange(o)
        setForm(undefined)
        setCopied(undefined)
        if (sends) onSent?.()
      })
      .catch((e) => setError(e.message))
      .finally(() => setBusy(false))
  }

  const save = (e: React.FormEvent) => {
    e.preventDefault()
    if (!form) return
    call('', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...form,
        items: form.items.map((i) => ({
          name: i.name.trim(),
          variant: i.variant.trim() || null,
          quantity: Number(i.quantity),
          unit_price: i.price === '' ? null : Math.round(Number(i.price) * 100),
        })),
      }),
    })
  }

  // Citations show as the message's position in the thread, with the text on hover.
  const src = (ids: string[]) =>
    ids.length === 0 ? (
      <sup title="Typed by the seller, not taken from the chat"> [seller]</sup>
    ) : (
      <sup>
        {ids.map((id) => {
          const n = messages.findIndex((m) => m.id === id)
          return (
            <abbr key={id} title={messages[n]?.text}>
              [{n + 1}]
            </abbr>
          )
        })}
      </sup>
    )

  // Read the actual words behind each value: a citation only proves the message exists, not that it says what the draft claims.
  const citedIds = new Set([
    ...draft.items.flatMap((i) => i.sources),
    ...FIELDS.flatMap(([key]) => draft[key].sources),
    ...draft.claims.flatMap((c) => c.sources),
  ])
  const cited = messages.map((m, k) => ({ n: k + 1, m })).filter(({ m }) => citedIds.has(m.id))

  const cancel = () => {
    const reason = prompt('Cancel this order? The record is kept. Reason (optional):')
    if (reason === null) return
    call('/cancel', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reason }) })
  }

  const setItem = (n: number, patch: Partial<Form['items'][number]>) =>
    setForm((f) => f && { ...f, items: f.items.map((i, k) => (k === n ? { ...i, ...patch } : i)) })

  return (
    <section className="order">
      <h2>
        Order · version {order.version} · {STATUS[order.status]}
      </h2>

      {order.cancelled && (
        <p role="status">
          Cancelled by {order.cancelled.by} on {new Date(order.cancelled.at).toLocaleString()}.
          {order.cancelled.reason && <> Reason: {order.cancelled.reason}</>}
        </p>
      )}
      {confirmation?.kind === 'customer' && (
        <p role="status">
          Customer confirmed version {confirmation.version}{' '}
          {confirmation.by === 'chat' ? 'by tapping Confirm in the chat' : 'through the link'} on{' '}
          {new Date(confirmation.at).toLocaleString()}.
        </p>
      )}
      {order.status === 'awaiting_confirmation' && order.asked?.confirm?.version === order.version && (
        <p role="status">
          Sent for confirmation in the chat. Waiting for the customer to tap <strong>{order.asked.confirm.text}</strong>.{' '}
          <button disabled={busy} onClick={() => call('/ask-confirmation', { method: 'POST' }, true)}>
            Send again
          </button>
        </p>
      )}
      {confirmation?.kind === 'seller_asserted' && (
        <p role="status" className="claim">
          <strong>Not confirmed by the customer.</strong> {confirmation.by} recorded that the customer agreed to version{' '}
          {confirmation.version} on {new Date(confirmation.at).toLocaleString()}.
        </p>
      )}
      {earlier.length > 0 && (
        <p>
          <small>
            Earlier agreements:{' '}
            {earlier
              .map(
                (c) =>
                  `version ${c.version} (${c.kind === 'customer' ? 'customer' : 'seller-reported'}, ${new Date(c.at).toLocaleDateString()})`,
              )
              .join('; ')}
            .{order.status !== 'confirmed' && ' The order changed since, so it needs confirming again.'}
          </small>
        </p>
      )}

      {form ? (
        <form onSubmit={save}>
          {form.items.map((i, n) => (
            <fieldset key={n}>
              <legend>Item {n + 1}</legend>
              <label>
                Name <input required value={i.name} onChange={(e) => setItem(n, { name: e.target.value })} />
              </label>
              <label>
                Variant <input value={i.variant} onChange={(e) => setItem(n, { variant: e.target.value })} />
              </label>
              <label>
                Quantity{' '}
                <input
                  required
                  type="number"
                  min="1"
                  step="1"
                  value={i.quantity}
                  onChange={(e) => setItem(n, { quantity: e.target.value })}
                />
              </label>
              <label>
                Unit price (₱){' '}
                <input type="number" min="0" step="0.01" value={i.price} onChange={(e) => setItem(n, { price: e.target.value })} />
              </label>
              <button type="button" onClick={() => setForm({ ...form, items: form.items.filter((_, k) => k !== n) })}>
                Remove item
              </button>
            </fieldset>
          ))}
          <button
            type="button"
            onClick={() => setForm({ ...form, items: [...form.items, { name: '', variant: '', quantity: '1', price: '' }] })}
          >
            Add item
          </button>
          {FIELDS.map(([key, label]) => (
            <label key={key}>
              {label} <input value={form[key]} onChange={(e) => setForm({ ...form, [key]: e.target.value })} />
            </label>
          ))}
          {order.status !== 'draft' && <p>Saving a change creates a new version, and the customer will need to confirm again.</p>}
          <button disabled={busy}>{busy ? 'Saving…' : 'Save'}</button>{' '}
          <button type="button" onClick={() => setForm(undefined)}>
            Cancel
          </button>
        </form>
      ) : (
        <>
          {!!order.catalog_notes?.length && (
            <ul className="catalog-notes">
              {order.catalog_notes.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
          )}
          <table>
            <thead>
              <tr>
                <th>Item</th>
                <th>Qty</th>
                <th>Unit price</th>
              </tr>
            </thead>
            <tbody>
              {draft.items.map((i, n) => (
                <tr key={n}>
                  <td>
                    {i.name}
                    {i.variant && ` (${i.variant})`}
                    {src(i.sources)}
                  </td>
                  <td>{i.quantity}</td>
                  <td>
                    {peso(i.unit_price)}
                    {i.price_withheld && <small className="withheld"> None of your messages states this price. Enter it with Edit.</small>}
                    {i.price_from === 'catalog' && (
                      <small className="from-catalog"> From your catalog: no message states this price.</small>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <th colSpan={2}>Total</th>
                <td>{draft.total === null ? 'Unknown' : peso(draft.total)}</td>
              </tr>
            </tfoot>
          </table>
          <dl>
            {FIELDS.map(([key, label]) => (
              <div key={key}>
                <dt>{label}</dt>
                <dd>
                  {draft[key].value ? (
                    <>
                      {draft[key].value}
                      {draft[key].from_order ? (
                        <sup title="Not said in this part of the chat: copied from this customer's earlier order, which they confirmed. Check it is still right.">
                          {' '}
                          [earlier order]
                        </sup>
                      ) : (
                        src(draft[key].sources)
                      )}
                    </>
                  ) : (
                    <em>Missing</em>
                  )}
                </dd>
              </div>
            ))}
          </dl>
          <p>
            <small>Numbers in brackets are the chat messages each value came from.</small>
          </p>
          {cited.length > 0 && (
            <details className="cited">
              <summary>Cited messages ({cited.length})</summary>
              {cited.map(({ n, m }) => (
                <blockquote key={m.id}>
                  <strong>
                    [{n}] {m.from_page ? 'You' : 'Customer'}:
                  </strong>{' '}
                  {m.text}
                </blockquote>
              ))}
            </details>
          )}
        </>
      )}

      {checks && fromChat(order) && <Signals conversationId={order.source.conversation_id} messages={messages} />}
      {!fromChat(order) &&
        draft.claims.map((c, n) => (
          <p key={n} className="claim">
            <strong>Unverified {c.kind} claim.</strong> {c.text}
            {src(c.sources)}
            {c.reference && <> Reference: {c.reference}.</>}
            {c.pressure && <> The customer is asking you to act before this is verified.</>}
          </p>
        ))}
      {draft.clarification_questions.length > 0 && (
        <>
          <h3>Ask the customer</h3>
          <ul>
            {draft.clarification_questions.map((q) => (
              <li key={q}>{q}</li>
            ))}
          </ul>
          <button onClick={() => setMessage(draft.clarification_questions.join('\n'))}>Write these to the customer</button>
        </>
      )}

      {error && <p role="alert">{error}</p>}
      {!form && order.status !== 'cancelled' && (
        <>
          {order.missing.length > 0 && <p>Before asking the customer to confirm, fill in: {order.missing.join(', ')}.</p>}
          {(order.status === 'confirmed' || payment) && (
            <div className={payment?.status === 'verified' ? undefined : 'claim'}>
              <h3>Payment {payment?.livemode === false && <mark>TEST</mark>}</h3>
              {!payment && (
                <p>
                  Not requested yet.{' '}
                  {fromChat(order)
                    ? 'Send the PayMongo link in the chat, or ask for direct GCash below.'
                    : 'The customer pays from the link below.'}
                </p>
              )}
              {fromChat(order) && order.status === 'confirmed' && (!payment || payment.status === 'expired') && (
                <p>
                  <button className="primary" disabled={busy} onClick={() => call('/payment-link', { method: 'POST' }, true)}>
                    Send payment link in chat
                  </button>
                </p>
              )}
              {payment?.status === 'pending' && <p>Waiting for payment of {peso(payment.amount)}. Nothing has been received.</p>}
              {payment?.status === 'expired' && <p>The payment request expired unpaid. The customer can start again from the link.</p>}
              {payment?.status === 'verified' && (
                <p>
                  <strong>Verified by PayMongo:</strong> {peso(payment.paid_amount ?? null)} received ({payment.payment_id}).
                  {payment.version !== order.version && (
                    <> This was for version {payment.version}; the order has changed since and needs review.</>
                  )}
                </p>
              )}
              {payment?.status === 'mismatched' && (
                <p>
                  <strong>Needs review:</strong> {payment.problem}. Expected {peso(payment.amount)}, received{' '}
                  {peso(payment.paid_amount ?? null)} ({payment.payment_id}).
                </p>
              )}
              {paymentClaim && payment?.status !== 'verified' && (
                <p>
                  The customer says they already paid{paymentClaim.reference && <> (reference {paymentClaim.reference})</>}.{' '}
                  <strong>No matching verified payment.</strong> Verify before you ship.
                </p>
              )}
              {order.payment_review?.map((r) => (
                <p key={r.payment_id}>
                  <strong>Unexpected payment:</strong> {peso(r.paid_amount)} ({r.payment_id}). {r.reason}.
                </p>
              ))}
              {payment && (
                <p>
                  <small>
                    {payment.checked_at
                      ? `Last checked with PayMongo ${new Date(payment.checked_at).toLocaleString()}.`
                      : 'Not checked with PayMongo yet.'}
                  </small>{' '}
                  <button disabled={busy} onClick={() => call('/check-payment', { method: 'POST' })}>
                    Recheck payment
                  </button>
                </p>
              )}
            </div>
          )}
          <GcashPayment order={order} onChange={onChange} write={setMessage} onSent={onSent} />
          <Packing order={order} onChange={onChange} />
          <Dispatch order={order} onChange={onChange} onSent={onSent} chat={chat} />
          <Claims order={order} onChange={onChange} write={setMessage} chat={chat} />
          {link && fromChat(order) && (
            <details>
              <summary>Customer page, if the chat can't be used</summary>
              <p>
                <small>
                  For when Messenger's 24-hour reply window has closed: the same order summary, payment, delivery status and problem report,
                  on a page of its own.
                </small>
                <br />
                <input readOnly value={link} onFocus={(e) => e.target.select()} aria-label="Customer page link" />{' '}
                <button onClick={() => navigator.clipboard.writeText(link).then(() => setCopied('link'))}>
                  {copied === 'link' ? 'Copied' : 'Copy'}
                </button>
              </p>
            </details>
          )}
          {link && !fromChat(order) && (
            <p>
              {order.status === 'confirmed' ? 'Customer link (order summary and payment):' : 'Send this link to the customer:'}
              <br />
              <input readOnly value={link} onFocus={(e) => e.target.select()} aria-label="Confirmation link" />{' '}
              <button onClick={() => navigator.clipboard.writeText(link).then(() => setCopied('link'))}>
                {copied === 'link' ? 'Copied' : 'Copy'}
              </button>{' '}
              <button
                onClick={() =>
                  setMessage(
                    order.status === 'confirmed'
                      ? `Thank you for confirming! You can pay ${peso(draft.total)} with QR Ph here: ${link}`
                      : `Hi! Here is your order summary. Please check it and tap the button to confirm: ${link}`,
                  )
                }
              >
                Write link to customer
              </button>
            </p>
          )}
          {fromChat(order) && message && (
            <Reply conversationId={order.source.conversation_id} orderId={order.id} text={message} setText={setMessage} onSent={onSent} />
          )}
          <p className="actions">
            <button onClick={() => setForm(toForm(draft))}>Edit</button>
            <button onClick={() => navigator.clipboard.writeText(details).then(() => setCopied('details'))}>
              {copied === 'details' ? 'Copied' : 'Copy order details'}
            </button>
            {order.status === 'draft' &&
              (fromChat(order) ? (
                <button
                  className="primary"
                  disabled={busy || order.missing.length > 0}
                  onClick={() => call('/ask-confirmation', { method: 'POST' }, true)}
                >
                  Send for confirmation in chat
                </button>
              ) : (
                <button disabled={busy || order.missing.length > 0} onClick={() => call('/send-confirmation', { method: 'POST' })}>
                  Create confirmation link
                </button>
              ))}
            {order.status !== 'confirmed' && (
              <button
                disabled={busy || order.missing.length > 0}
                onClick={() =>
                  confirm('Record that the customer agreed, without their own confirmation?') &&
                  call('/assert-confirmation', { method: 'POST' })
                }
              >
                Customer agreed another way
              </button>
            )}
            <button disabled={busy} onClick={cancel}>
              Cancel order
            </button>
          </p>
        </>
      )}
    </section>
  )
}
