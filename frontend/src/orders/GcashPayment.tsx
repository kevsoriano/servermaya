import { useState } from 'react'
import { load } from '../lib/firebase'
import { fromChat, localMobile, peso } from '../lib/format'
import type { Order } from './OrderView'
import { Photo } from './Packing'
import { LABEL, RESULT, shown, type Check, type Claim } from '../lib/receiptChecks'

export type ManualPayment = {
  status: 'requested' | 'claimed' | 'seller_confirmed'
  version: number
  amount: number
  pay_to: { name: string; mobile: string; masked_name?: string | null; user_id?: string | null }
  requested_at: string
  /** says_paid: the customer said they already paid, so receipts count from counts_from, when the conversation began. */
  kind?: 'ask' | 'says_paid'
  counts_from?: string
  claims: Claim[]
  history_check?: { status: 'found' | 'amount_differs' | 'not_found'; checked_at: string } | null
  history_read?: { reference: string | null; entry: { invno: string | null }; checks: Check[] } | null
  confirmed: { reference: string; invno?: string | null; claim_id: string | null; history_image?: object | null; by: string; at: string; reason: string | null } | null
}

const HISTORY = {
  found: 'Found in your GCash history',
  amount_differs: 'In your GCash history with a different amount',
  not_found: 'Not in your GCash history, although the export covers it',
}

/** The guardrails, with the values this request expects, so the seller can check any screenshot by eye, including bank-app receipts. */
function Guardrails({ m }: { m: ManualPayment }) {
  const from = new Date(new Date(m.requested_at).getTime() - 2 * 60_000).toLocaleString()
  return (
    <details open={m.claims.length === 0}>
      <summary>What to check on a receipt</summary>
      <dl>
        <dt>{LABEL.outcome}</dt>
        <dd>The screen says successful, not failed or pending</dd>
        <dt>{LABEL.amount}</dt>
        <dd>Exactly {peso(m.amount)}, centavos included</dd>
        <dt>{LABEL.time}</dt>
        <dd>From {from} (Philippine time) until the screenshot was sent</dd>
        <dt>{LABEL.reference}</dt>
        <dd>
          13 digits from GCash, or 6 digits from a bank app such as BPI, and not used on another order. Find the payment in your own GCash history: for a
          bank payment, GCash shows its own reference, and the invno starts with the transfer date and ends in the bank&apos;s 6 digits.
        </dd>
        <dt>{LABEL.recipient_name}</dt>
        <dd>
          {m.pay_to.masked_name ? (
            <>
              <code>{m.pay_to.masked_name}</code>, which is how GCash shows {m.pay_to.name}
            </>
          ) : (
            <>{m.pay_to.name}, with some letters hidden by GCash</>
          )}
        </dd>
        <dt>{LABEL.recipient_mobile}</dt>
        <dd>
          {localMobile(m.pay_to.mobile)}, with some digits hidden by GCash
          {m.pay_to.user_id && (
            <>
              . Bank apps show the account instead: it ends in <code>{m.pay_to.user_id.slice(-3)}</code>
            </>
          )}
        </dd>
        <dt>{LABEL.duplicate}</dt>
        <dd>Not the same screenshot as another order, and not the same amount and minute as another receipt with a different reference</dd>
      </dl>
      <p>
        <small>A screenshot that passes all of these can still be fake.</small>
      </p>
    </details>
  )
}

/** Direct GCash, for customers who will not use the PayMongo link. Screenshots are claims; only the seller's own GCash history confirms. */
export default function GcashPayment({ order, onChange, write }: { order: Order; onChange: (o: Order) => void; write: (text: string) => void }) {
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [reference, setReference] = useState('')
  const [reason, setReason] = useState('')
  const [invno, setInvno] = useState('')
  const [found, setFound] = useState(false)
  const m = order.manual_payment
  const current = m && m.version === order.version ? m : null
  const paidByPayMongo = order.payment?.status === 'verified' && order.payment.version === order.version

  if (order.status !== 'confirmed' || (paidByPayMongo && !current)) return null

  const run = (label: string, path: string, body?: object | File) => {
    setBusy(label)
    setError('')
    return load<Order>(`/orders/${order.id}/gcash${path}`, {
      method: 'POST',
      ...(body instanceof File
        ? { headers: { 'Content-Type': body.type }, body }
        : body && { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
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

  // The customer says they already paid: nothing to ask for, only proof to match against the seller's own GCash history.
  const saysPaid = () =>
    run('Saving…', '/request', { says_paid: true }).then((o) => {
      if (o?.manual_payment) write('Thank you! Please send a screenshot of your GCash receipt here so we can match it with our GCash history.')
    })

  // The seller's screenshot of the payment in their own GCash history fills in the form; the seller still confirms.
  const fill = (o?: Order) => {
    const h = o?.manual_payment?.history_read
    if (h) {
      setReference(h.reference ?? '')
      setInvno(h.entry.invno ?? '')
    }
  }
  const readHistory = (file?: File) => file && run('Reading your screenshot…', '/history', file).then(fill)
  // Demo only: the demo seller's GCash history entry for the newest matching sample receipt, read like an upload.
  const demo = order.source?.type === 'demo'
  const sampleHistory = () => run('Reading the sample screenshot…', '/sample-history', {}).then(fill)

  const tail = invno.replace(/\W/g, '').toUpperCase()
  // Mirrors receipts.confirm: the invno starts with the transfer date (or the next day) and ends in the bank's 6 digits.
  const days = (at: string) => [0, 1].map((d) => new Date(Date.parse(`${at.slice(0, 10)}T00:00Z`) + d * 864e5).toISOString().slice(0, 10).replace(/-/g, ''))
  const matching = current?.claims.findLast(
    (c) =>
      c.reference &&
      (c.reference === reference.replace(/\s/g, '') ||
        (tail && c.reference.length === 6 && tail.endsWith(c.reference) && !!c.receipt.paid_at && days(c.receipt.paid_at).includes(tail.slice(0, 8)))),
  )
  const fromBank = current?.claims.some((c) => c.reference?.length === 6)
  const history = current?.history_read?.reference === reference.replace(/\s/g, '') ? current.history_read : null
  const historyDoubt = !!history?.checks.some((c) => c.result !== 'pass')
  const doubtful = !matching || matching.checks.some((c) => c.result !== 'pass') || historyDoubt

  return (
    <div className={current?.status === 'seller_confirmed' ? undefined : 'claim'}>
      <h3>Direct GCash</h3>
      {m && !current && <p>A GCash request was made for version {m.version}. The order has changed since; ask again for this version.</p>}
      {!current && (
        <p>
          For customers who will not use the payment link.{' '}
          <button disabled={!!busy} onClick={ask}>
            Ask for GCash payment
          </button>{' '}
          <button disabled={!!busy} onClick={saysPaid}>
            They say they already paid
          </button>
        </p>
      )}
      {current && current.kind !== 'says_paid' && current.status !== 'seller_confirmed' && (
        <p>
          <small>
            Customer says they paid before you asked?{' '}
            <button disabled={!!busy} onClick={saysPaid}>
              Count receipts from the start of the conversation
            </button>
          </small>
        </p>
      )}
      {current && (
        <>
          {current.kind === 'says_paid' && current.counts_from ? (
            <p>
              The customer says they already paid {peso(current.amount)} to {current.pay_to.name} ({localMobile(current.pay_to.mobile)}). What they say is not proof: only a
              payment you find in your own GCash history confirms it. Receipts dated before {new Date(current.counts_from).toLocaleString()}, when the conversation about this
              order began, do not count.
            </p>
          ) : (
            <p>
              Asked for {peso(current.amount)} to {current.pay_to.name} ({localMobile(current.pay_to.mobile)}) on {new Date(current.requested_at).toLocaleString()}. Receipts dated
              before then do not count.
            </p>
          )}
          <Guardrails m={current} />
          {current.claims.map((c) => (
            <div key={c.id} className="receipt">
              <p>
                <strong>Screenshot sent {new Date(c.received_at).toLocaleString()}</strong>
                {c.reference && <> (reference {c.reference})</>}
              </p>
              <Photo path={`/orders/${order.id}/gcash/claims/${c.id}/image`} alt="Receipt screenshot from the customer" caption="Compare the screenshot with each row" />
              <table>
                <thead>
                  <tr>
                    <th>Check</th>
                    <th>Screenshot shows</th>
                    <th>Result</th>
                  </tr>
                </thead>
                <tbody>
                  {c.checks.map((k) => (
                    <tr key={k.check}>
                      <td>{LABEL[k.check] ?? k.check}</td>
                      <td>{shown(c.receipt, k.check)}</td>
                      <td>
                        <span className={`result ${k.result}`}>{RESULT[k.result]}</span>
                        <br />
                        <small>{k.detail}</small>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
          {current.status === 'seller_confirmed' && current.confirmed ? (
            <p>
              <strong>Seller confirmed in GCash</strong> by {current.confirmed.by} on {new Date(current.confirmed.at).toLocaleString()}: reference {current.confirmed.reference}
              {current.confirmed.reason && <> ({current.confirmed.reason})</>}. Not verified by PayMongo.
              {current.history_check && (
                <>
                  <br />
                  <span className={`result ${current.history_check.status === 'found' ? 'pass' : 'fail'}`}>{HISTORY[current.history_check.status]}</span>{' '}
                  <small>(GCash history uploaded {new Date(current.history_check.checked_at).toLocaleString()})</small>
                </>
              )}
            </p>
          ) : (
            <>
              <p className="actions">
                <button disabled={!!busy || !fromChat(order)} onClick={() => run('Reading screenshots… about 10 seconds each', '/read')}>
                  Check receipt screenshots
                </button>
              </p>
              <form
                onSubmit={(e) => {
                  e.preventDefault()
                  run('Saving…', '/confirm', { reference, invno, reason, found_in_history: found })
                }}
              >
                <p>
                  <small>Passing checks do not prove payment; a good fake passes them all. Open your GCash app and find the payment in your transaction history.</small>
                </p>
                <label>
                  Faster: upload a screenshot of this payment in your GCash history
                  <input type="file" accept="image/*" disabled={!!busy} onChange={(e) => (readHistory(e.target.files?.[0]), (e.target.value = ''))} />
                </label>
                {demo && (
                  <p className="demo-note">
                    <small>
                      Demo: you have no GCash app for the demo shop. Use{' '}
                      <button type="button" disabled={!!busy} onClick={sampleHistory}>
                        Sample history screenshot
                      </button>{' '}
                      for its entry of a Matching sample receipt, or the sample history PDF in Settings. A customer's receipt uploaded here
                      fails on purpose: it shows money sent, not money received.
                    </small>
                  </p>
                )}
                {history && (
                  <ul>
                    {history.checks.map((k) => (
                      <li key={k.check}>
                        <span className={`result ${k.result}`}>{RESULT[k.result]}</span>: {k.detail}
                      </li>
                    ))}
                  </ul>
                )}
                <label>
                  Reference number in your GCash history
                  <input inputMode="numeric" maxLength={40} value={reference} onChange={(e) => setReference(e.target.value)} />
                </label>
                {fromBank && (
                  <label>
                    Paid from a bank app? The invno in your GCash history
                    <input maxLength={80} value={invno} onChange={(e) => setInvno(e.target.value)} />
                  </label>
                )}
                {reference && doubtful && (
                  <label>
                    {historyDoubt
                      ? 'Your GCash history screenshot did not pass every check. Why are you confirming?'
                      : matching
                        ? 'Some checks did not pass. Why are you confirming?'
                        : 'No checked screenshot shows this reference. Why are you confirming?'}
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
