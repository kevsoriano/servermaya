import { useEffect, useState } from 'react'
import { load } from '../lib/firebase'
import { CLAIM_TYPES as TYPES, peso } from '../lib/format'

type Ratio = { numerator: number; denominator: number; rate: number | null; of: string }
type Review = { status: string; reason?: string; by?: string; at?: string; was?: string }
type Incident = {
  order_id?: string
  claim_id?: string
  type?: string
  at?: string
  outcome?: string | null
  conversation_id?: string | null
  name?: string | null
  pressure?: boolean
}
export type Card = {
  key: string
  rule: 'rising' | 'payment_reference_reuse' | 'recurring_customer_claims' | 'loss_concentration' | 'contents_concentration'
  entity: { kind: string; value: string; label: string }
  incidents: Incident[]
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
  // rising cards: cases per week, oldest first (the last is the last 7 days), and new orders per week for scale
  weeks?: number[]
  weekly_before?: number
  volume?: number[]
  unit?: string
  with_pressure?: number | null
}

const RULE = {
  rising: 'Rising this week',
  payment_reference_reuse: 'Payment reference reused',
  recurring_customer_claims: 'Recurring claims from one customer',
  loss_concentration: 'Damage and non-receipt concentration',
  contents_concentration: 'Missing or replaced contents',
}
const WEEKS = ['3 weeks ago', '2 weeks ago', 'Last week', 'Last 7 days']
const ago = (at?: string) => {
  const days = at ? Math.floor((Date.now() - new Date(at).getTime()) / 86_400_000) : NaN
  return Number.isNaN(days) ? '' : days < 1 ? 'today' : days < 2 ? 'yesterday' : `${days} days ago`
}

/** How a rising card's last 7 days compare with the weeks before, and the chats behind them. */
function Rising({ card, onOpenChat }: { card: Card; onOpenChat?: (id: string) => void }) {
  const weeks = card.weeks ?? []
  const top = Math.max(...weeks, 1)
  return (
    <>
      <p className="trend-count">
        <strong>{card.numerator}</strong> {card.unit} in the last 7 days, against {card.weekly_before} a week in the 3 weeks before.
        {card.with_pressure != null && card.with_pressure > 0 && (
          <>
            {' '}
            {card.with_pressure === card.numerator ? 'Each' : `${card.with_pressure} of them`} also asked you to ship before the payment was
            confirmed.
          </>
        )}
      </p>
      <ol className="trend-bars" aria-label="Cases per week">
        {weeks.map((n, i) => (
          <li key={i} className={i === weeks.length - 1 ? 'now' : undefined}>
            <span className="n">{n}</span>
            <span className="bar" style={{ height: `${(n / top) * 100}%` }} />
            <small>{WEEKS[i]}</small>
          </li>
        ))}
      </ol>
      <ul className="trend-cases">
        {card.incidents.map((i) => (
          <li key={i.claim_id ?? i.order_id ?? `${i.conversation_id}${i.at}`}>
            {i.conversation_id && onOpenChat ? (
              <button className="link" onClick={() => onOpenChat(i.conversation_id!)}>
                {i.name ?? 'A chat'}
              </button>
            ) : (
              <span>{i.name ?? `Order ${i.order_id}`}</span>
            )}{' '}
            <small>{ago(i.at)}</small>
          </li>
        ))}
      </ul>
    </>
  )
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
        </small>
      </p>
    </div>
  )
}

export function PatternCard({
  card,
  compact,
  onReviewed,
  onOpenChat,
  before,
}: {
  card: Card
  compact?: boolean
  onReviewed?: (r: Review) => void
  onOpenChat?: (id: string) => void
  before?: number | null // its count when the seller last looked: null if it was not there, undefined if they never looked
}) {
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
    <div className={`signal pattern ${card.rule}`}>
      <p>
        {card.rule === 'rising' ? (
          <strong>{card.entity.label}</strong>
        ) : (
          <>
            <strong>{RULE[card.rule]}</strong> · {card.entity.label}
          </>
        )}{' '}
        · <mark>{REVIEW[card.review.status] ?? card.review.status}</mark>
        {card.elevated && card.rule !== 'rising' && <mark> Higher than the rest of the store</mark>}
        {before === null && <mark className="since"> Appeared since your last look</mark>}
        {before != null && before < card.numerator && <mark className="since"> Up from {before} since your last look</mark>}
      </p>
      {card.rule === 'rising' ? (
        <Rising card={card} onOpenChat={onOpenChat} />
      ) : card.denominator != null ? (
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
      {card.rule !== 'rising' && (
        <p>
          Linked:{' '}
          {card.incidents.map((i, n) => (
            <span key={i.claim_id ?? i.order_id}>
              {n > 0 && ', '}
              {i.name && i.conversation_id && onOpenChat ? (
                <button className="link" onClick={() => onOpenChat(i.conversation_id!)}>
                  {i.name}
                </button>
              ) : (
                (i.name ?? `order ${i.order_id}`)
              )}
              {i.type && <small> ({TYPES[i.type] ?? i.type})</small>}
            </span>
          ))}
        </p>
      )}
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

// Each card's count when this seller last looked, kept in this browser only, to mark what is new or up since then.
const SEEN = 'patterns-seen'
function lastSeen(): Record<string, number> | undefined {
  try {
    const raw = localStorage.getItem(SEEN)
    return raw ? JSON.parse(raw) : undefined
  } catch {
    return undefined
  }
}
function remember(cards: Card[]) {
  try {
    localStorage.setItem(SEEN, JSON.stringify(Object.fromEntries(cards.map((c) => [c.key, c.numerator]))))
  } catch {
    // storage blocked: the markers just do not show
  }
}

/** Concentrations worth a look, computed from the store's own orders and chats. Cards never block or decide anything. */
export default function Patterns({ onOpenChat }: { onOpenChat?: (id: string) => void }) {
  const [days, setDays] = useState(30)
  const [rule, setRule] = useState('')
  const [state, setState] = useState('')
  const [result, setResult] = useState<{ days: number; cards: Card[] }>()
  const [error, setError] = useState('')
  const [returns, setReturns] = useState<Returns>()
  const [seen] = useState(lastSeen) // as it was when the page opened
  const cards = result?.days === days ? result.cards : undefined
  const setCards = (c: Card[] | undefined) => c && setResult({ days, cards: c })

  useEffect(() => {
    load<Returns>(`/patterns/returns?days=${days}`).then(setReturns, () => setReturns(undefined))
    load<Card[]>(`/patterns?days=${days}`).then(
      (c) => (setResult({ days, cards: c }), remember(c)),
      (e) => setError(e.message),
    )
  }, [days])

  const shown = (cards ?? []).filter((c) => (!rule || c.rule === rule) && (!state || c.review.status === state))
  const rising = shown.filter((c) => c.rule === 'rising')
  const rest = shown.filter((c) => c.rule !== 'rising')
  const card = (c: Card) => (
    <PatternCard
      key={c.key}
      card={c}
      before={seen ? (seen[c.key] ?? null) : undefined}
      onOpenChat={onOpenChat}
      onReviewed={(r) => setCards(cards?.map((x) => (x.key === c.key ? { ...x, review: r, previous_review: undefined } : x)))}
    />
  )
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
        {!cards && !error && <p>Loading…</p>}
        {cards && shown.length === 0 && <p>Nothing to look into for these filters.</p>}
        {rising.length > 0 && (
          <>
            <h3>Rising this week</h3>
            <p className="muted">The last 7 days against the 3 weeks before, whatever the window above.</p>
            {rising.map(card)}
          </>
        )}
        {rest.length > 0 && (
          <>
            <h3>Concentrations and repeats</h3>
            {rest.map(card)}
          </>
        )}
        {returns?.days === days && <ReturnsByCause r={returns} />}
      </section>
    </div>
  )
}
