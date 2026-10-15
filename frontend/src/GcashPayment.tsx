import { useState } from 'react'
import { load, localMobile, peso } from './firebase'
import type { Order } from './OrderView'
import { Photo } from './Packing'

type Check = { check: string; result: 'pass' | 'fail' | 'unreadable'; detail: string }
type Receipt = {
  readable: boolean
  outcome: 'successful' | 'failed' | 'pending' | 'not_shown'
  amount_php: number | null
  reference: string | null
  paid_at: string | null
  recipient_name: string | null
  recipient_mobile: string | null
  recipient_account?: string | null
}
type Claim = { id: string; message_id: string; received_at: string; reference: string | null; receipt: Receipt; checks: Check[] }
export type ManualPayment = {
  status: 'requested' | 'claimed' | 'seller_confirmed'
  version: number
  amount: number
  pay_to: { name: string; mobile: string; masked_name?: string | null; user_id?: string | null }
  requested_at: string
  claims: Claim[]
  confirmed: { reference: string; invno?: string | null; claim_id: string | null; by: string; at: string; reason: string | null } | null
}

const RESULT = { pass: 'Pass', fail: 'Does not fit', unreadable: 'Could not read' }

const LABEL: Record<string, string> = {
  receipt: 'Receipt',
  outcome: 'Payment went through',
  amount: 'Amount',
  time: 'Date and time',
  reference: 'Reference number',
  recipient_name: 'Recipient name',
  recipient_mobile: 'Recipient number or account',
  duplicate: 'Not a copy',
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
          bank payment, GCash shows its own reference, and the invno ends in the bank&apos;s 6 digits.
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

/** What the screenshot shows for each check, as Gemini copied it down. */
function shown(r: Receipt, check: string) {
  const value = {
    outcome: r.outcome === 'not_shown' ? null : r.outcome,
    amount: r.amount_php === null ? null : peso(Math.round(r.amount_php * 100)),
    time: r.paid_at?.replace('T', ' '),
    reference: r.reference,
    recipient_name: r.recipient_name,
    recipient_mobile: r.recipient_mobile ?? r.recipient_account,
  }[check]
  return value === undefined ? '' : (value ?? 'nothing readable')
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

  const tail = invno.replace(/\W/g, '').toUpperCase()
  const matching = current?.claims.findLast(
    (c) => c.reference && (c.reference === reference.replace(/\s/g, '') || (tail && c.reference.length === 6 && tail.endsWith(c.reference))),
  )
  const fromBank = current?.claims.some((c) => c.reference?.length === 6)
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
                  run('Saving…', '/confirm', { reference, invno, reason, found_in_history: found })
                }}
              >
                <p>
                  <small>Passing checks do not prove payment; a good fake passes them all. Open your GCash app and find the payment in your transaction history.</small>
                </p>
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
