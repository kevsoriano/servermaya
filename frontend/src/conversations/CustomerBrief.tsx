import { type ReactNode, useEffect, useState } from 'react'
import { load } from '../lib/firebase'

type Brief = { summary: string | null; problem?: string }

const ABOUT =
  "AI summary of this customer's chat and their orders with your shop, from your records. It describes what happened; it is never a judgement of the customer and never blocks an order."

/** Who this customer is to the shop, above the checks: a short AI-written brief from their chat and orders.
 * changed: anything that should refresh it, such as the newest message and the orders' states; undefined until the orders
 * have loaded, so the brief is asked for once. */
export default function CustomerBrief({
  conversationId,
  name,
  avatar,
  changed,
}: {
  conversationId: string
  name: string
  avatar: ReactNode
  changed?: string
}) {
  const [brief, setBrief] = useState<{ id: string; at: string; b: Brief }>()
  const [error, setError] = useState('')
  const [tries, setTries] = useState(0) // Try again: a brief that could not be written is never kept, so asking again retries

  useEffect(() => {
    if (changed === undefined) return
    load<Brief>(`/conversations/${conversationId}/brief`).then(
      (b) => (setBrief({ id: conversationId, at: changed, b }), setError('')),
      (e) => setError(e.message),
    )
  }, [conversationId, changed, tries])

  const b = brief?.id === conversationId ? brief.b : undefined
  return (
    <section className="brief" aria-label="About this customer">
      {avatar}
      <h3>
        {name}{' '}
        <span className="info" tabIndex={0} role="img" aria-label={ABOUT} data-tip={ABOUT}>
          ⓘ
        </span>
      </h3>
      <div className="brief-text">
        {b?.summary ? <p>{b.summary}</p> : !b && !error ? <p className="muted">Reading their history…</p> : null}
        {(b?.problem || error) && (
          <p className="muted">
            {b?.problem ?? error} <button onClick={() => (setBrief(undefined), setError(''), setTries((n) => n + 1))}>Try again</button>
          </p>
        )}
      </div>
    </section>
  )
}
