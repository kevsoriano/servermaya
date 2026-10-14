import { useEffect, useState } from 'react'
import { load } from './firebase'

type Signal = {
  key: string
  kind: string
  summary: string
  sources: string[]
  check: string
  resolved: string | null
  reviewed: { by: string; at: string; note: string } | null
}

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
  const [reviewing, setReviewing] = useState<string>()
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)

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

  const isOpen = (s: Signal) => !s.resolved && !s.reviewed
  const open = signals?.filter(isOpen) ?? []
  const done = signals?.filter((s) => !isOpen(s)) ?? []

  const review = (body: { key: string; note?: string; reopen?: boolean }) => {
    setBusy(true)
    setError('')
    load<Signal[]>(`/conversations/${conversationId}/signals/review`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
      .then((s) => {
        setSignals(s)
        setReviewing(undefined)
        setNote('')
      })
      .catch((e) => setError(e.message))
      .finally(() => setBusy(false))
  }
  const quote = (id: string) => messages.find((m) => m.id === id)?.text

  return (
    <section className="signals" aria-label="Checks before shipping">
      <h3>Checks before shipping{signals && ` (${open.length} open)`}</h3>
      {error && <p role="alert">{error}</p>}
      {!signals && !error && <p>Reading the conversation…</p>}
      {signals?.length === 0 && <p>Nothing in this conversation needs checking.</p>}
      {[...open, ...done].map((s) => (
        <div key={s.key} className={isOpen(s) ? 'signal' : 'signal done'}>
          <strong>{LABEL[s.kind] ?? s.kind}</strong>
          <p>{s.summary}</p>
          {s.sources.map((id) => quote(id) && <blockquote key={id}>{quote(id)}</blockquote>)}
          {s.resolved ? (
            <p>Settled: {s.resolved}.</p>
          ) : s.reviewed ? (
            <p>
              Reviewed by {s.reviewed.by} on {new Date(s.reviewed.at).toLocaleString()}: {s.reviewed.note}{' '}
              <button disabled={busy} onClick={() => review({ key: s.key, reopen: true })}>
                Reopen
              </button>
            </p>
          ) : (
            <p>Check: {s.check}</p>
          )}
          {isOpen(s) &&
            (reviewing === s.key ? (
              <form
                onSubmit={(e) => {
                  e.preventDefault()
                  review({ key: s.key, note })
                }}
              >
                <label>
                  Why is this fine?
                  <input required autoFocus value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} />
                </label>
                <button disabled={busy || !note.trim()}>Mark reviewed</button>{' '}
                <button type="button" onClick={() => setReviewing(undefined)}>
                  Cancel
                </button>
              </form>
            ) : (
              <button onClick={() => (setReviewing(s.key), setNote(''))}>Mark reviewed…</button>
            ))}
        </div>
      ))}
      <p>
        <small>AI reading of what was said, to help you decide what to verify. Not a judgement of the customer, and it never blocks an order.</small>
      </p>
    </section>
  )
}
