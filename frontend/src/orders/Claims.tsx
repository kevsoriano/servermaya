import { useEffect, useState } from 'react'
import { load } from '../lib/firebase'
import { CLAIM_TYPES as TYPES, peso } from '../lib/format'
import type { ChatMessage, Order } from './OrderView'
import { Photo } from './Packing'
import { PatternCard, type Card } from '../patterns/Patterns'

type Finding = {
  statement: string
  status: 'supported' | 'conflicting' | 'unverified' | 'insufficient_data'
  evidence: string[]
  uncertainty: string
  withheld?: boolean
}
type Action = 'clarification' | 'refund' | 'partial_refund' | 'replacement' | 'redelivery' | 'escalation'
export type Claim = {
  id: string
  type: string
  narrative: string
  source: 'customer_link' | 'customer_chat' | 'seller'
  message_ids?: string[]
  created_by: string
  created_at: string
  status: 'open' | 'awaiting_evidence' | 'under_review' | 'resolution_approved' | 'executing' | 'resolved' | 'escalated'
  photos: { id: string; source: string }[]
  /** Such as the customer's unboxing video. The review watches each one and checks it is one continuous shot. */
  videos?: { id: string; source: string }[]
  review: {
    findings: Finding[]
    recommendation: { action: Action; amount_php: number | null; explanation: string; evidence: string[] }
    draft_reply: string
    questions: string[]
    sources?: Record<string, string>
    read?: string[] // the records the agent fetched with its tools; missing on reviews made before it had them
    model: string
    at: string
  } | null
  review_stale?: boolean
  decision: {
    action: Action
    amount: number | null
    reason: string
    reply: string | null
    by: string
    at: string
    followed_ai: boolean
  } | null
  resolution: { how: string; note: string | null; by: string; at: string } | null
  escalation: { owner: string; next_action: string } | null
}
export type Refund = {
  id: string
  claim_id: string | null
  method: 'paymongo_api' | 'paymongo_dashboard' | 'outside_paymongo'
  amount: number
  status: string
  provider_refund_id: string | null
  reference?: string | null
  problem?: string
  by: string
  at: string
}

const ACTIONS: Record<Action, string> = {
  clarification: 'Ask for clarification',
  refund: 'Full refund',
  partial_refund: 'Partial refund',
  replacement: 'Send a replacement',
  redelivery: 'Deliver again',
  escalation: 'Escalate',
}
const FINDING = { supported: 'Supported', conflicting: 'Conflicting', unverified: 'Unverified', insufficient_data: 'Insufficient data' }
const REFUND: Record<string, string> = {
  submitting: 'Submitting to PayMongo',
  uncertain: 'No answer from PayMongo: check status before anything else',
  pending: 'Pending at PayMongo',
  succeeded: 'Succeeded at PayMongo',
  failed: 'Failed at PayMongo',
  api_refused: 'PayMongo refused an API refund',
  awaiting_reconcile: 'Made in the PayMongo Dashboard: check status to confirm',
  seller_reported: 'Seller-reported, not verified by PayMongo',
  not_found: 'Never reached PayMongo',
}

function Review({ c, onDecide }: { c: Claim; onDecide: (a: Action, amount: number | null, reply: string) => void }) {
  const r = c.review!
  return (
    <div className="review">
      <p>
        <small>
          AI review by {r.model}, {new Date(r.at).toLocaleString()}.
          {r.read && <> It read: {r.read.map((x) => (x === 'past' ? 'past decisions' : x)).join(', ') || 'nothing'}.</>} Every statement
          cites the records below it; check them before you decide.
          {c.review_stale && <strong> New evidence arrived since: review again.</strong>}
        </small>
      </p>
      <ul className="findings">
        {r.findings.map((f, n) => (
          <li key={n} className={`finding ${f.status}`}>
            <span className={`result ${f.status === 'supported' ? 'pass' : f.status === 'conflicting' ? 'fail' : 'unreadable'}`}>
              {FINDING[f.status]}
            </span>
            : {f.statement}
            {f.evidence.length > 0 && (
              <>
                {' '}
                {f.evidence.map((e) => (
                  <abbr key={e} title={r.sources?.[e]}>
                    [{e}]
                  </abbr>
                ))}
              </>
            )}
            {f.uncertainty && <small> · Could be wrong if: {f.uncertainty}</small>}
          </li>
        ))}
      </ul>
      <p>
        Suggested: <strong>{ACTIONS[r.recommendation.action]}</strong>
        {r.recommendation.amount_php != null && <> of {peso(Math.round(r.recommendation.amount_php * 100))}</>}.{' '}
        {r.recommendation.explanation}{' '}
        {r.recommendation.evidence.map((e) => (
          <abbr key={e} title={r.sources?.[e]}>
            [{e}]
          </abbr>
        ))}
      </p>
      {r.questions.length > 0 && (
        <details>
          <summary>Questions to ask</summary>
          <ul>
            {r.questions.map((q) => (
              <li key={q}>{q}</li>
            ))}
          </ul>
        </details>
      )}
      {!c.decision && (
        <button
          onClick={() =>
            onDecide(
              r.recommendation.action,
              r.recommendation.amount_php != null ? Math.round(r.recommendation.amount_php * 100) : null,
              r.draft_reply,
            )
          }
        >
          Use as a starting point
        </button>
      )}
    </div>
  )
}

function RefundPanel({ order, c, run }: { order: Order; c: Claim; run: (label: string, path: string, body?: object) => Promise<unknown> }) {
  const decided = c.decision!.amount!
  const mine = (order.refunds ?? []).filter((r) => r.claim_id === c.id)
  const held = mine.filter((r) => !['failed', 'api_refused', 'not_found'].includes(r.status)).reduce((s, r) => s + r.amount, 0)
  const left = Math.min(decided - held, order.refundable)
  const [method, setMethod] = useState<'outside_paymongo' | 'paymongo_dashboard'>('outside_paymongo')
  const [reference, setReference] = useState('')
  const viaPayMongo = order.payment?.status === 'verified'
  const refused = mine.some((r) => r.status === 'api_refused')

  return (
    <div>
      <p>
        Approved refund {peso(decided)}. Still to refund: <strong>{peso(Math.max(left, 0))}</strong>. The payment can still refund{' '}
        {peso(order.refundable)}.
      </p>
      {mine.length > 0 && (
        <ul>
          {mine.map((r) => (
            <li key={r.id}>
              {peso(r.amount)} · {REFUND[r.status] ?? r.status}
              {r.reference && <> · ref {r.reference}</>}
              {r.problem && <small> ({r.problem})</small>}
            </li>
          ))}
        </ul>
      )}
      {viaPayMongo && mine.some((r) => ['uncertain', 'pending', 'submitting', 'awaiting_reconcile'].includes(r.status)) && (
        <button onClick={() => run('Checking with PayMongo…', '/refunds/check')}>Check refund status with PayMongo</button>
      )}
      {left > 0 && (
        <>
          {viaPayMongo && !refused && (
            <p>
              <button
                className="primary"
                onClick={() =>
                  confirm(`Refund ${peso(left)} to the customer through PayMongo? This moves money.`) &&
                  run('Submitting to PayMongo…', '/refunds', { claim_id: c.id, amount: left, reason: 'requested_by_customer' })
                }
              >
                Refund {peso(left)} through PayMongo
              </button>
            </p>
          )}
          {(refused || !viaPayMongo) && (
            <form
              onSubmit={(e) => {
                e.preventDefault()
                run('Saving…', '/refunds/manual', { claim_id: c.id, amount: left, method, reference })
              }}
            >
              <p>
                {refused
                  ? 'PayMongo does not refund QR Ph payments through its API. Refund another way, then record it here.'
                  : 'This payment did not go through PayMongo. Refund the customer yourself, then record it here.'}
              </p>
              {viaPayMongo && (
                <label>
                  How you refunded
                  <select value={method} onChange={(e) => setMethod(e.target.value as typeof method)}>
                    <option value="outside_paymongo">Outside PayMongo (GCash or bank transfer)</option>
                    <option value="paymongo_dashboard">In the PayMongo Dashboard</option>
                  </select>
                </label>
              )}
              <label>
                Transfer reference{method === 'paymongo_dashboard' && viaPayMongo ? ' (optional)' : ''}
                <input
                  value={reference}
                  onChange={(e) => setReference(e.target.value)}
                  required={method === 'outside_paymongo' || !viaPayMongo}
                />
              </label>
              <button>Record refund of {peso(left)}</button>
              <p>
                <small>A refund outside PayMongo is shown as seller-reported. It is never marked as verified.</small>
              </p>
            </form>
          )}
        </>
      )}
    </div>
  )
}

function ClaimCard({ order, c, onChange, write }: { order: Order; c: Claim; onChange: (o: Order) => void; write: (t: string) => void }) {
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [deciding, setDeciding] = useState<{ action: Action; amount: string; reason: string; reply: string }>()
  const [note, setNote] = useState('')
  const [escalating, setEscalating] = useState(false)
  const [owner, setOwner] = useState('')
  const [next, setNext] = useState('')

  const run = (label: string, path: string, body?: object | File) => {
    setBusy(label)
    setError('')
    const claimPath = path.startsWith('/refunds') ? path : `/claims/${c.id}${path}`
    return load<Order>(`/orders/${order.id}${claimPath}`, {
      method: 'POST',
      ...(body instanceof File
        ? { headers: { 'Content-Type': body.type }, body }
        : body && { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
    })
      .then(onChange, (e) => setError(e.message))
      .finally(() => setBusy(''))
  }
  const decide = (action: Action, amount: number | null, reply: string) =>
    setDeciding({ action, amount: amount ? (amount / 100).toFixed(2) : '', reason: '', reply })
  const open = c.status !== 'resolved'
  const act = c.decision?.action

  return (
    <div className={`claim ${open ? '' : 'done'}`}>
      <p>
        <strong>{TYPES[c.type]}</strong> · {c.status.replace(/_/g, ' ')} · from{' '}
        {c.source === 'customer_link'
          ? 'the customer (order link)'
          : c.source === 'customer_chat'
            ? `the customer's chat messages, opened by ${c.created_by}`
            : c.created_by}
        , {new Date(c.created_at).toLocaleString()}
      </p>
      <blockquote>{c.narrative}</blockquote>
      {(c.photos.length > 0 || !!c.videos?.length) && (
        <div className="photos">
          {c.photos.map((p, n) => (
            <Photo
              key={p.id}
              path={`/orders/${order.id}/claims/${c.id}/photos/${p.id}`}
              alt={`Claim photo ${n + 1}`}
              caption={`CP${n + 1} · ${p.source}`}
            />
          ))}
          {c.videos?.map((v, n) => (
            <Photo
              key={v.id}
              video
              path={`/orders/${order.id}/claims/${c.id}/photos/${v.id}`}
              alt={`Claim video ${n + 1}`}
              caption={`CV${n + 1} · ${v.source}`}
            />
          ))}
        </div>
      )}
      {open && c.photos.length < 4 && (
        <label className="upload">
          Add a photo or video to this report
          <input
            type="file"
            accept="image/*,video/mp4,video/quicktime,video/webm"
            disabled={!!busy}
            onChange={(e) => e.target.files?.[0] && run('Uploading…', '/photos', e.target.files[0])}
          />
        </label>
      )}

      {c.review && <Review c={c} onDecide={decide} />}
      {open && !c.decision && (
        <p className="actions">
          <button
            disabled={!!busy}
            onClick={() =>
              run(`Reviewing the records… about ${c.videos?.length || order.packing?.videos?.length ? 30 : 20} seconds`, '/review')
            }
          >
            {c.review ? 'Review again' : 'Review with AI'}
          </button>
          {['open', 'under_review'].includes(c.status) && (
            <button disabled={!!busy} onClick={() => run('Saving…', '/wait')}>
              Waiting for the customer’s evidence
            </button>
          )}
          {!deciding && <button onClick={() => decide('clarification', null, '')}>Decide without AI</button>}
        </p>
      )}

      {deciding && !c.decision && (
        <form
          onSubmit={(e) => {
            e.preventDefault()
            const amount = deciding.action === 'partial_refund' ? Math.round(parseFloat(deciding.amount) * 100) : null
            run('Saving…', '/decide', { action: deciding.action, amount, reason: deciding.reason, reply: deciding.reply }).then(() =>
              setDeciding(undefined),
            )
          }}
        >
          <label>
            Outcome
            <select value={deciding.action} onChange={(e) => setDeciding({ ...deciding, action: e.target.value as Action })}>
              {Object.entries(ACTIONS).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
          </label>
          {deciding.action === 'partial_refund' && (
            <label>
              Amount in pesos
              <input
                required
                inputMode="decimal"
                value={deciding.amount}
                onChange={(e) => setDeciding({ ...deciding, amount: e.target.value })}
              />
            </label>
          )}
          <label>
            Why (kept on record)
            <input required value={deciding.reason} onChange={(e) => setDeciding({ ...deciding, reason: e.target.value })} />
          </label>
          <label>
            Reply to the customer (edit before sending)
            <textarea rows={3} value={deciding.reply} onChange={(e) => setDeciding({ ...deciding, reply: e.target.value })} />
          </label>
          <p>
            <small>Approving records your decision. It does not refund, ship or message anyone by itself.</small>
          </p>
          <button className="primary" disabled={!!busy}>
            Approve outcome
          </button>{' '}
          <button type="button" onClick={() => setDeciding(undefined)}>
            Cancel
          </button>
        </form>
      )}

      {c.decision && (
        <p>
          Approved: <strong>{ACTIONS[c.decision.action]}</strong>
          {c.decision.amount != null && <> of {peso(c.decision.amount)}</>} by {c.decision.by} ({c.decision.reason})
          {c.decision.followed_ai ? ' · same as the AI suggestion' : c.review ? ' · differs from the AI suggestion' : ''}.{' '}
          {c.decision.reply && <button onClick={() => write(c.decision!.reply!)}>Write the reply to the customer</button>}
        </p>
      )}
      {open && (act === 'refund' || act === 'partial_refund') && <RefundPanel order={order} c={c} run={run} />}
      {open && (act === 'replacement' || act === 'redelivery') && !order.replacement && c.status === 'resolution_approved' && (
        <button disabled={!!busy} onClick={() => run('Starting…', '/replacement')}>
          Start the {act === 'replacement' ? 'replacement (pack and ship again)' : 'redelivery'}
        </button>
      )}
      {order.replacement?.claim_id === c.id && (
        <p>In progress: pack and dispatch it in the panels above. The original shipment stays on record.</p>
      )}

      {c.escalation && (
        <p>
          Escalated to <strong>{c.escalation.owner}</strong>. Next: {c.escalation.next_action}
        </p>
      )}
      {open && c.decision && (
        <form
          onSubmit={(e) => {
            e.preventDefault()
            run('Closing…', '/resolve', { note })
          }}
        >
          <label>
            Close the report once the outcome has happened
            {['clarification', 'escalation'].includes(act!) ? ': how it was resolved' : ' (note optional)'}
            <input value={note} onChange={(e) => setNote(e.target.value)} />
          </label>
          <button disabled={!!busy}>Close report</button>
        </form>
      )}
      {open && !escalating && c.status !== 'escalated' && <button onClick={() => setEscalating(true)}>Escalate</button>}
      {escalating && open && (
        <form
          onSubmit={(e) => {
            e.preventDefault()
            run('Saving…', '/escalate', { owner, next_action: next }).then(() => setEscalating(false))
          }}
        >
          <label>
            Owner
            <input required value={owner} onChange={(e) => setOwner(e.target.value)} />
          </label>
          <label>
            Next action
            <input required value={next} onChange={(e) => setNext(e.target.value)} />
          </label>
          <button>Escalate</button>
        </form>
      )}
      {c.resolution && (
        <p role="status">
          Closed by {c.resolution.by} on {new Date(c.resolution.at).toLocaleString()}: {c.resolution.how}
          {c.resolution.note && <> ({c.resolution.note})</>}.
        </p>
      )}
      {error && <p role="alert">{error}</p>}
      {busy && <p role="status">{busy}</p>}
    </div>
  )
}

/** Claims on this order. Each review cites the order's records; the seller decides and carries out every outcome.
 * chat: the live thread, so a complaint made there can become a claim in the customer's own words and photos. */
export default function Claims({
  order,
  onChange,
  write,
  chat,
}: {
  order: Order
  onChange: (o: Order) => void
  write: (t: string) => void
  chat?: ChatMessage[]
}) {
  const [adding, setAdding] = useState(false)
  const [type, setType] = useState('damage')
  const [narrative, setNarrative] = useState('')
  const [picked, setPicked] = useState<string[]>([])
  const [error, setError] = useState('')
  const [cards, setCards] = useState<Card[]>([])
  const claims = order.claims ?? []
  const count = claims.length

  useEffect(() => {
    if (count) load<Card[]>(`/orders/${order.id}/patterns`).then(setCards, () => setCards([]))
  }, [order.id, count])

  if (order.status !== 'confirmed' && order.status !== 'cancelled') return null

  // The customer's messages since the order was drafted, leaving out taps on Confirm or Received.
  const taps = new Set([order.asked?.confirm?.text, order.asked?.received?.text])
  const drafted = Date.parse(order.messages.at(-1)?.created_time ?? '')
  const theirs = (chat ?? []).filter((m) => !m.from_page && Date.parse(m.created_time) > drafted && !taps.has(m.text.trim()))
  // Written since it was sent (and since they said they received it) and not yet in a claim: maybe a problem to report.
  const sentAt = Date.parse([order.fulfillment, ...(order.fulfillment_history ?? [])].find(Boolean)?.events[0]?.at ?? '')
  const after = Math.max(sentAt, Date.parse(order.receipt?.at ?? '') || 0)
  const inClaims = new Set(claims.flatMap((c) => c.message_ids ?? []))
  const since = theirs.filter((m) => Date.parse(m.created_time) > after && !inClaims.has(m.id))

  const startAdding = () => (setAdding(true), setPicked(since.map((m) => m.id)))

  const add = (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    const [path, body] = picked.length ? ['/claims/from-chat', { type, message_ids: picked }] : ['/claims', { type, narrative }]
    load<Order>(`/orders/${order.id}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(
      (o) => (onChange(o), setAdding(false), setNarrative(''), setPicked([])),
      (e) => setError(e.message),
    )
  }

  return (
    <section className="packing">
      <h3>Customer claims{count > 0 && ` · ${claims.filter((c) => c.status !== 'resolved').length} open`}</h3>
      {!adding && since.length > 0 && (
        <p className="claim">
          The customer wrote in the chat since the order was sent. If it's about a problem, open a claim from those messages.{' '}
          <button onClick={startAdding}>Open claim from the chat</button>
        </p>
      )}
      {cards.map((card) => (
        <PatternCard key={card.key} card={card} compact />
      ))}
      {[...claims].reverse().map((c) => (
        <ClaimCard key={c.id} order={order} c={c} onChange={onChange} write={write} />
      ))}
      {adding ? (
        <form onSubmit={add}>
          <label>
            What the customer reports
            <select value={type} onChange={(e) => setType(e.target.value)}>
              {Object.entries(TYPES).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
          </label>
          {theirs.length > 0 && (
            <fieldset>
              <legend>The customer's messages about it: their words and photos become the claim</legend>
              {theirs.map((m) => (
                <label key={m.id} className="pick">
                  <input
                    type="checkbox"
                    checked={picked.includes(m.id)}
                    onChange={(e) => setPicked((p) => (e.target.checked ? [...p, m.id] : p.filter((x) => x !== m.id)))}
                  />{' '}
                  {m.text || <em>No text</em>}
                  {m.attachments.some((a) => a.kind === 'image') && <small> · photo</small>}
                  {m.attachments.some((a) => a.kind === 'video') && <small> · video</small>}{' '}
                  <small>({new Date(m.created_time).toLocaleString()})</small>
                </label>
              ))}
            </fieldset>
          )}
          {!picked.length && (
            <label>
              {theirs.length ? 'Or in your words' : 'In their words, or yours'}
              <textarea required rows={3} maxLength={2000} value={narrative} onChange={(e) => setNarrative(e.target.value)} />
            </label>
          )}
          <button>Open claim</button>{' '}
          <button type="button" onClick={() => setAdding(false)}>
            Cancel
          </button>
        </form>
      ) : (
        <button onClick={startAdding}>Record a claim</button>
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  )
}
