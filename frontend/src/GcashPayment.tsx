import { useState } from 'react'
import { load, localMobile, peso } from './firebase'
import type { Order } from './OrderView'

type Check = { check: string; result: 'pass' | 'fail' | 'unreadable'; detail: string }
type Claim = { id: string; message_id: string; received_at: string; reference: string | null; checks: Check[] }
export type ManualPayment = {
  status: 'requested' | 'claimed' | 'seller_confirmed'
  version: number
  amount: number
  pay_to: { name: string; mobile: string; masked_name?: string | null }
  requested_at: string
  claims: Claim[]
  confirmed: { reference: string; claim_id: string | null; by: string; at: string; reason: string | null } | null
}

const RESULT = { pass: 'Pass', fail: 'Does not fit', unreadable: 'Could not read' }

/** Direct GCash, for customers who will not use the PayMongo link. Screenshots are claims; only the seller's own GCash history confirms. */
export default function GcashPayment({ order, onChange, write }: { order: Order; onChange: (o: Order) => void; write: (text: string) => void }) {
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [reference, setReference] = useState('')
  const [reason, setReason] = useState('')
  const [found, setFound] = useState(false)
  const m = order.manual_payment
  const current = m && m.version === order.version ? m : null
  const paidByPayMongo = order.payment?.status === 'verified' && order.payment.version === order.version

  if (order.status !== 'confirmed' || (paidByPayMongo && !current)) return null

  const run = (label: string, path: string, body?: object) => {
    setBusy(label)
    setError('')
    return load<Order>(`/orders/${order.id}/gcash${path}`, {
      method: 'POST',
      ...(body && { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
    })
      .then((o) => (onChange(o), o))
      .catch((e) => (setError(e.message), undefined))
      .finally(() => setBusy(''))
  }

  const ask = () =>
    run('Saving…', '/request').then((o) => {
      const p = o?.manual_payment
      if (p) write(`You can also pay ${peso(p.amount)} by GCash to ${p.pay_to.name}, ${localMobile(p.pay_to.mobile)}. Please send a screenshot of the receipt here after paying.`)
    })

  const matching = current?.claims.findLast((c) => c.reference && c.reference === reference.replace(/\s/g, ''))
  const doubtful = !matching || matching.checks.some((c) => c.result !== 'pass')

  return (
    <div className={current?.status === 'seller_confirmed' ? undefined : 'claim'}>
      <h3>Direct GCash</h3>
      {m && !current && <p>A GCash request was made for version {m.version}. The order has changed since; ask again for this version.</p>}
      {!current && (
        <p>
          For customers who will not use the payment link.{' '}
          <button disabled={!!busy} onClick={ask}>
            Ask for GCash payment
          </button>
        </p>
      )}
      {current && (
        <>
          <p>
            Asked for {peso(current.amount)} to {current.pay_to.name} ({localMobile(current.pay_to.mobile)}) on {new Date(current.requested_at).toLocaleString()}. Receipts dated
            before then do not count.
          </p>
          {current.claims.map((c) => (
            <div key={c.id}>
              <p>
                <strong>Screenshot sent {new Date(c.received_at).toLocaleString()}</strong>
                {c.reference && <> (reference {c.reference})</>}
              </p>
              <ul>
                {c.checks.map((k) => (
                  <li key={k.check}>
                    <span className={`result ${k.result}`}>{RESULT[k.result]}</span>: {k.detail}
                  </li>
                ))}
              </ul>
            </div>
          ))}
          {current.status === 'seller_confirmed' && current.confirmed ? (
            <p>
              <strong>Seller confirmed in GCash</strong> by {current.confirmed.by} on {new Date(current.confirmed.at).toLocaleString()}: reference {current.confirmed.reference}
              {current.confirmed.reason && <> ({current.confirmed.reason})</>}. Not verified by PayMongo.
            </p>
          ) : (
            <>
              <p className="actions">
                <button disabled={!!busy || order.source?.type !== 'messenger'} onClick={() => run('Reading screenshots… about 10 seconds each', '/read')}>
                  Check receipt screenshots
                </button>
              </p>
              <form
                onSubmit={(e) => {
                  e.preventDefault()
                  run('Saving…', '/confirm', { reference, reason, found_in_history: found })
                }}
              >
                <p>
                  <small>Passing checks do not prove payment; a good fake passes them all. Open your GCash app and find the payment in your transaction history.</small>
                </p>
                <label>
                  Reference number in your GCash history
                  <input inputMode="numeric" maxLength={40} value={reference} onChange={(e) => setReference(e.target.value)} />
                </label>
                {reference && doubtful && (
                  <label>
                    {matching ? 'Some checks did not pass. Why are you confirming?' : 'No checked screenshot shows this reference. Why are you confirming?'}
                    <input maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
                  </label>
                )}
                <label>
                  <input type="checkbox" checked={found} onChange={(e) => setFound(e.target.checked)} /> I found this payment of {peso(current.amount)} in my own GCash
                  history
                </label>
                <button disabled={!!busy || !found || !reference.trim() || (doubtful && !reason.trim())}>Confirm GCash payment</button>
              </form>
            </>
          )}
        </>
      )}
      {error && <p role="alert">{error}</p>}
      {busy && <p role="status">{busy}</p>}
    </div>
  )
}
