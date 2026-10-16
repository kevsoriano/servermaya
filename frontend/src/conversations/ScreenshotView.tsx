import { useEffect, useRef, useState } from 'react'
import { load } from '../lib/firebase'
import { localMobile, peso } from '../lib/format'
import type { ManualPayment } from '../orders/GcashPayment'
import { LABEL, RESULT, shown, type Claim } from '../lib/receiptChecks'
import type { Order } from '../orders/OrderView'

const MARK = { pass: '✓', fail: '✗', unreadable: '?' }

/** The GCash request this chat image was checked against, if any: the current one or an earlier version's. */
function checkedAs(orders: Order[], messageId: string) {
  for (const order of orders) {
    for (const m of [order.manual_payment, ...(order.manual_payment_history ?? [])]) {
      const claim = m?.claims.find((c) => c.message_id === messageId)
      if (m && claim) return { order, m, claim, current: m === order.manual_payment && m.version === order.version }
    }
  }
}

/** What the order's own records say about payment. Never read from the screenshot. */
function paymentStatus(order: Order, m: ManualPayment, claim: Claim) {
  const p = order.payment
  if (p?.status === 'verified' && p.version === order.version)
    return { ok: true, text: `Paid: PayMongo verified ${peso(p.paid_amount ?? p.amount)}.` }
  if (m.status === 'seller_confirmed' && m.confirmed) {
    return m.confirmed.claim_id === claim.id
      ? { ok: true, text: `Confirmed: reference ${m.confirmed.reference} was found in your GCash history. Not verified by PayMongo.` }
      : {
          ok: true,
          text: `This order's GCash payment was confirmed with another screenshot (reference ${m.confirmed.reference}), not this one.`,
        }
  }
  return { ok: false, text: 'Not yet found in your GCash history. Open GCash and look for this reference before you ship.' }
}

/** An image from the chat, enlarged. A customer's GCash screenshot also shows its checks, next to the order's real payment status. */
export default function ScreenshotView({
  url,
  alt,
  messageId,
  fromCustomer,
  orders,
  onOrderChanged,
  onClose,
}: {
  url: string
  alt: string
  messageId: string
  fromCustomer: boolean
  orders: Order[]
  onOrderChanged: (o: Order) => void
  onClose: () => void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => dialog.current?.showModal(), [])

  const found = checkedAs(orders, messageId)
  // A screenshot is read once a GCash request is open on this chat's order.
  const waiting =
    !found && fromCustomer
      ? orders.find((o) => o.manual_payment?.version === o.version && o.manual_payment.status !== 'seller_confirmed')
      : undefined

  const check = () => {
    if (!waiting) return
    setBusy(true)
    setError('')
    load<Order>(`/orders/${waiting.id}/gcash/read`, { method: 'POST' })
      .then(onOrderChanged, (e) => setError(e.message))
      .finally(() => setBusy(false))
  }

  return (
    <dialog ref={dialog} className="viewer" onClose={onClose} onClick={(e) => e.target === dialog.current && dialog.current.close()}>
      <div className={found || waiting ? 'viewer-body with-checks' : 'viewer-body'}>
        <figure>
          <img src={url} alt={alt} />
          <figcaption>
            <a href={url} target="_blank" rel="noreferrer">
              Open original
            </a>
          </figcaption>
        </figure>
        {found &&
          (() => {
            const { order, m, claim, current } = found
            const status = paymentStatus(order, m, claim)
            return (
              <section aria-label="Checks on this screenshot">
                <h2>Customer&apos;s screenshot: a claim, not proof of payment</h2>
                <p className={status.ok ? 'viewer-status ok' : 'viewer-status open'} role="status">
                  {status.text}
                </p>
                {!status.ok && order.source?.type === 'demo' && (
                  <p className="demo-note">
                    <small>
                      Demo: you have no GCash app for the demo shop. Use <strong>Sample history screenshot</strong> in the order, or the
                      sample history PDF in Settings.
                    </small>
                  </p>
                )}
                <ul className="checks">
                  {claim.checks.map((k) => (
                    <li key={k.check}>
                      <span className={`result ${k.result}`} aria-label={RESULT[k.result]} title={RESULT[k.result]}>
                        {MARK[k.result]}
                      </span>
                      <div>
                        <strong>{LABEL[k.check] ?? k.check}</strong>
                        {/* The value explains a cross or a question mark; on a pass the reason already says it. */}
                        {k.result !== 'pass' && shown(claim.receipt, k.check) && (
                          <small> · screenshot shows {shown(claim.receipt, k.check)}</small>
                        )}
                        <p>{k.detail}</p>
                      </div>
                    </li>
                  ))}
                </ul>
                <p>
                  <small>
                    Checked against {peso(m.amount)} to {m.pay_to.name}, {localMobile(m.pay_to.mobile)}, on the order of{' '}
                    {new Date(order.created_at).toLocaleDateString()}
                    {current ? '' : ' (an earlier version of that order)'}. Passing checks do not prove payment: a good fake passes them
                    all.
                  </small>
                </p>
              </section>
            )
          })()}
        {waiting && (
          <section aria-label="Checks on this screenshot">
            <h2>Not checked yet</h2>
            <p>If this is a GCash receipt, check it against the open GCash request for {peso(waiting.manual_payment!.amount)}.</p>
            <button className="primary" disabled={busy} onClick={check}>
              {busy ? 'Reading screenshots… about 10 seconds each' : 'Check receipt screenshots'}
            </button>
            {error && <p role="alert">{error}</p>}
          </section>
        )}
      </div>
      <button className="viewer-close" onClick={() => dialog.current?.close()} aria-label="Close">
        ×
      </button>
    </dialog>
  )
}
