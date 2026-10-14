import { useEffect, useState } from 'react'
import { load } from './firebase'

type Signal = { kind: string; summary: string; sources: string[]; check: string; resolved: string | null }

const LABEL: Record<string, string> = {
  pressure: 'Asked to ship before payment is verified',
  payment_claim: 'Says they already paid',
  changed_payment_details: 'Changed payment or refund details',
  inconsistent_details: 'Details changed during the chat',
  receipt_image: 'Sent a receipt image as proof',
}

type Props = {
  conversationId: string
  /** Messages to quote from, by id. */
  messages: { id: string; text: string }[]
  /** Changes when a new message arrives, so the checks are refreshed. */
  latest?: string
}

/** What to verify before shipping, each tied to the messages it came from. Never a verdict on the customer. */
export default function Signals({ conversationId, messages, latest }: Props) {
  const [signals, setSignals] = useState<Signal[]>()
  const [error, setError] = useState('')

  useEffect(() => {
    let live = true
    load<Signal[]>(`/conversations/${conversationId}/signals`).then(
      (s) => live && (setSignals(s), setError('')),
      (e) => live && setError(e.message),
    )
    return () => {
      live = false
    }
  }, [conversationId, latest])

  const open = signals?.filter((s) => !s.resolved) ?? []
  const done = signals?.filter((s) => s.resolved) ?? []
  const quote = (id: string) => messages.find((m) => m.id === id)?.text

  return (
    <section className="signals" aria-label="Checks before shipping">
      <h3>Checks before shipping{signals && ` (${open.length} open)`}</h3>
      {error && <p role="alert">{error}</p>}
      {!signals && !error && <p>Reading the conversation…</p>}
      {signals?.length === 0 && <p>Nothing in this conversation needs checking.</p>}
      {[...open, ...done].map((s, n) => (
        <div key={n} className={s.resolved ? 'signal done' : 'signal'}>
          <strong>{LABEL[s.kind] ?? s.kind}</strong>
          <p>{s.summary}</p>
          {s.sources.map((id) => quote(id) && <blockquote key={id}>{quote(id)}</blockquote>)}
          <p>{s.resolved ? <>Settled: {s.resolved}.</> : <>Check: {s.check}</>}</p>
        </div>
      ))}
      <p>
        <small>AI reading of what was said, to help you decide what to verify. Not a judgement of the customer, and it never blocks an order.</small>
      </p>
    </section>
  )
}
