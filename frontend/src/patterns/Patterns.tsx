import { useEffect, useState } from 'react'
import { load } from '../lib/firebase'
import { CLAIM_TYPES as TYPES, peso } from '../lib/format'

type Ratio = { numerator: number; denominator: number; rate: number | null; of: string }
type Review = { status: string; reason?: string; by?: string; at?: string; was?: string }
export type Card = {
  key: string
  rule: 'payment_reference_reuse' | 'recurring_customer_claims' | 'loss_concentration'
  entity: { kind: string; value: string; label: string }
  incidents: { order_id: string; claim_id?: string; type?: string; at?: string; outcome?: string | null }[]
  incidents_hash: string
  numerator: number
  denominator: number | null
  rate: number | null
  baseline: Ratio | null
  elevated: boolean
  outcomes?: Record<string, number>
  limitations: string[]
  alternatives: string[]
  suggested_review: string
  review: Review
  previous_review?: Review
}

const RULE = {
  payment_reference_reuse: 'Payment reference reused',
  recurring_customer_claims: 'Recurring claims from one customer',
  loss_concentration: 'Damage and non-receipt concentration',
}
const REVIEW: Record<string, string> = {
  new: 'New',
  reviewing: 'Reviewing',
  explained_operationally: 'Explained',
  unresolved: 'Unresolved',
  dismissed: 'Dismissed',
}
const pct = (r: number | null) => (r == null ? 'n/a' : `${Math.round(r * 100)}%`)

type Returns = {
  days: number
  delivered: number
  samples: number
  causes: Record<string, { count: number; rate: number | null; refunded: number; shipping: number; cost: number }>
  by: Record<'courier' | 'vehicle' | 'product', Record<string, Record<string, number>>>
}
const CAUSES: Record<string, string> = {
  courier_transit: 'Courier, in transit',
  seller_error: 'Our error',
  customer: 'The customer',
  unknown: 'Unknown',
}
const ORDER = ['courier_transit', 'seller_error', 'customer', 'unknown']

/** Claims that sent money or goods back, by the cause the seller recorded, with what they cost. */
function ReturnsByCause({ r }: { r: Returns }) {
  const causes = ORDER.filter((k) => r.causes[k])
  const total = causes.reduce((n, k) => n + r.causes[k].count, 0)
  return (
    <div className="signal">
      <p>
        <strong>Returns by cause</strong> · {total} return(s) against {r.delivered} delivered shipment(s), last {r.days} days
      </p>
      {total === 0 ? (
        <p>No refunds, replacements or redeliveries in this window.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Cause</th>
              <th>Returns</th>
              <th>Per delivered shipment</th>
              <th>Refunded</th>
              <th>Courier fees</th>
              <th>Cost</th>
            </tr>
          </thead>
          <tbody>
            {causes.map((k) => (
              <tr key={k}>
                <td>{CAUSES[k] ?? k}</td>
                <td>{r.causes[k].count}</td>
                <td>{pct(r.causes[k].rate)}</td>
                <td>{peso(r.causes[k].refunded)}</td>
                <td>{peso(r.causes[k].shipping)}</td>
                <td>{peso(r.causes[k].cost)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {total > 0 && (
        <details>
          <summary>By courier, vehicle and product</summary>
          {(['courier', 'vehicle', 'product'] as const).map((group) => (
            <table key={group}>
              <thead>
                <tr>
                  <th>{group[0].toUpperCase() + group.slice(1)}</th>
                  {causes.map((k) => (
                    <th key={k}>{CAUSES[k] ?? k}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {Object.entries(r.by[group]).map(([value, counts]) => (
                  <tr key={value}>
                    <td>{value}</td>
                    {causes.map((k) => (
                      <td key={k}>{counts[k] ?? 0}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          ))}
        </details>
      )}
      <p>
        <small>
          The cause is what you recorded on each claim; the custody check only suggests it. Cost is refunds made plus the courier fee of
          replacement and return shipments.
          {r.samples > 0 && ` Includes ${r.samples} sample orders with simulated riders, for the demo.`}
        </small>
      </p>
    </div>
  )
}

export function PatternCard({ card, compact, onReviewed }: { card: Card; compact?: boolean; onReviewed?: (r: Review) => void }) {
  const [status, setStatus] = useState('reviewing')
  const [reason, setReason] = useState('')
  const [error, setError] = useState('')

  const save = (e: React.FormEvent) => {
    e.preventDefault()
    load<Review>('/patterns/review', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: card.key, incidents_hash: card.incidents_hash, status, reason }),
    }).then(
      (r) => (onReviewed?.(r), setReason('')),
      (e) => setError(e.message),
    )
  }

  return (
    <div className="signal">
      <p>
        <strong>{RULE[card.rule]}</strong> · {card.entity.label} · <mark>{REVIEW[card.review.status] ?? card.review.status}</mark>
        {card.elevated && <mark> Higher than the rest of the store</mark>}
      </p>
      {card.denominator != null ? (
        <p>
          {card.numerator} of {card.denominator} delivered shipment(s) ({pct(card.rate)})
          {card.baseline && (
            <>
              {' '}
              against {card.baseline.numerator} of {card.baseline.denominator} for {card.baseline.of} ({pct(card.baseline.rate)})
            </>
          )}
          .
        </p>
      ) : (
        <p>{card.numerator} different orders received it.</p>
      )}
      {card.outcomes && (
        <p>
          <small>
            Outcomes:{' '}
            {Object.entries(card.outcomes)
              .map(([k, n]) => `${n} ${k === 'null' || !k ? 'not decided yet' : k.replace('_', ' ')}`)
              .join(', ')}
            . A claim is not a finding of error or abuse.
          </small>
        </p>
      )}
      <p>Linked: {card.incidents.map((i) => `order ${i.order_id}${i.type ? ` (${TYPES[i.type] ?? i.type})` : ''}`).join(', ')}</p>
      {!compact && (
        <>
          <p>Next: {card.suggested_review}</p>
          {card.limitations.length > 0 && (
            <ul>
              {card.limitations.map((l) => (
                <li key={l}>
                  <small>{l}</small>
                </li>
              ))}
            </ul>
          )}
          <details>
            <summary>Other explanations</summary>
            <ul>
              {card.alternatives.map((a) => (
                <li key={a}>{a}</li>
              ))}
            </ul>
          </details>
          {card.previous_review && (
            <p>
              <small>
                An earlier review ({REVIEW[card.previous_review.was ?? ''] ?? card.previous_review.was}: {card.previous_review.reason}) was
                superseded because the linked cases changed.
              </small>
            </p>
          )}
          {card.review.reason && (
            <p>
              <small>
                Reviewed by {card.review.by}: {card.review.reason}
              </small>
            </p>
          )}
          <form onSubmit={save}>
            <label>
              Review
              <select value={status} onChange={(e) => setStatus(e.target.value)}>
                {Object.entries(REVIEW)
                  .filter(([k]) => k !== 'new')
                  .map(([k, v]) => (
                    <option key={k} value={k}>
                      {v}
                    </option>
                  ))}
              </select>
            </label>
            <label>
              Reason (required)
              <input required value={reason} onChange={(e) => setReason(e.target.value)} />
            </label>
            <button>Save review</button>
          </form>
          {error && <p role="alert">{error}</p>}
        </>
      )}
      {compact && (
        <p>
          <small>Context only: it does not decide this claim. See the Patterns tab to review it.</small>
        </p>
      )}
    </div>
  )
}

/** Concentrations worth a look, computed from the store's own orders. Cards never block or decide anything. */
export default function Patterns() {
  const [days, setDays] = useState(30)
  const [rule, setRule] = useState('')
  const [state, setState] = useState('')
  const [result, setResult] = useState<{ days: number; cards: Card[] }>()
  const [error, setError] = useState('')
  const [returns, setReturns] = useState<Returns>()
  const cards = result?.days === days ? result.cards : undefined
  const setCards = (c: Card[] | undefined) => c && setResult({ days, cards: c })

  useEffect(() => {
    load<Returns>(`/patterns/returns?days=${days}`).then(setReturns, () => setReturns(undefined))
    load<Card[]>(`/patterns?days=${days}`).then(
      (c) => setResult({ days, cards: c }),
      (e) => setError(e.message),
    )
  }, [days])

  const shown = (cards ?? []).filter((c) => (!rule || c.rule === rule) && (!state || c.review.status === state))
  return (
    <div className="page">
      <section className="order">
        <h2>Patterns</h2>
        <p>
          Counts and rates from your own orders, for you to look into. They do not show intent, label anyone, or block a refund or shipment.
          Small numbers are marked as too little history.
        </p>
        <p className="actions">
          <label>
            Window{' '}
            <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
              {[7, 30, 90, 365].map((d) => (
                <option key={d} value={d}>
                  Last {d} days
                </option>
              ))}
            </select>
          </label>
          <label>
            Type{' '}
            <select value={rule} onChange={(e) => setRule(e.target.value)}>
              <option value="">All</option>
              {Object.entries(RULE).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
          </label>
          <label>
            Review{' '}
            <select value={state} onChange={(e) => setState(e.target.value)}>
              <option value="">All</option>
              {Object.entries(REVIEW).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
          </label>
        </p>
        {error && <p role="alert">{error}</p>}
        {returns?.days === days && <ReturnsByCause r={returns} />}
        {!cards && !error && <p>Loading…</p>}
        {cards && shown.length === 0 && <p>Nothing to look into for these filters.</p>}
        {shown.map((c) => (
          <PatternCard
            key={c.key}
            card={c}
            onReviewed={(r) => setCards(cards?.map((x) => (x.key === c.key ? { ...x, review: r, previous_review: undefined } : x)))}
          />
        ))}
      </section>
    </div>
  )
}
