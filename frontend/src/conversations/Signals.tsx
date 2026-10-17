import { useEffect, useState } from 'react'
import { load } from '../lib/firebase'

type Signal = {
  key: string
  kind: string
  summary: string
  sources: string[]
  check: string
  resolved: string | null
  reviewed: { by: string; at: string; note: string } | null
  /** On an earlier order that is received or cancelled: history, not something to verify now. */
  earlier: boolean
  /** Facts from the records beside a payment claim: payment details shared first or not, an earlier payment. */
  context?: string[]
}

const LABEL: Record<string, string> = {
  pressure: 'Asked to ship before payment is verified',
  payment_claim: 'Says they already paid',
  changed_payment_details: 'Changed payment or refund details',
  inconsistent_details: 'Details changed during the chat',
  receipt_image: 'Sent a receipt image as proof',
  account_request: 'Asked you to open an account, use a link or share a code',
}

type Props = {
  conversationId: string
  /** Messages to quote from, by id. */
  messages: { id: string; text: string }[]
  /** Changes when a new message arrives, so the checks are refreshed. */
  latest?: string
}

/** What to verify before shipping, each tied to the messages it came from. Never a verdict on the customer. */
const ABOUT =
  'AI reading of what was said, to help you decide what to verify. "Not seen" means the AI did not spot it, not that it was checked. Never a judgement of the customer, and it never blocks an order.'

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

  const isOpen = (s: Signal) => !s.resolved && !s.reviewed && !s.earlier
  const current = signals?.filter((s) => !s.earlier)
  const earlier = signals?.filter((s) => s.earlier) ?? []
  const open = signals?.filter(isOpen) ?? []
  // Every kind is listed, so the seller sees what was looked for. A kind with nothing found is "not seen", never a pass:
  // the AI missing something is not the same as code checking it.
  const found = current && [...open, ...current.filter((s) => !isOpen(s))]
  const unseen = Object.keys(LABEL).filter((kind) => !current?.some((s) => s.kind === kind))

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
      <h3>
        Checks before shipping{signals && ` (${open.length} to verify)`}{' '}
        <span className="info" tabIndex={0} role="img" aria-label={ABOUT} data-tip={ABOUT}>
          ⓘ
        </span>
      </h3>
      {error && <p role="alert">{error}</p>}
      {!signals && !error && <p>Reading the conversation…</p>}
      {found && (
        <ul className="checks">
          {found.map((s) => (
            <li key={s.key} className={isOpen(s) ? 'to-verify' : undefined}>
              <span
                className={`result ${isOpen(s) ? 'open' : 'pass'}`}
                aria-label={isOpen(s) ? 'To verify' : s.resolved ? 'Settled' : 'Reviewed'}
                title={isOpen(s) ? 'To verify' : s.resolved ? 'Settled' : 'Reviewed'}
              >
                {isOpen(s) ? '!' : '✓'}
              </span>
              <div>
                <strong>{LABEL[s.kind] ?? s.kind}</strong>
                {s.resolved ? (
                  <p>Settled: {s.resolved}.</p>
                ) : s.reviewed ? (
                  <p>
                    Reviewed by {s.reviewed.by}: {s.reviewed.note}{' '}
                    <button disabled={busy} onClick={() => review({ key: s.key, reopen: true })}>
                      Reopen
                    </button>
                  </p>
                ) : (
                  s.sources.slice(0, 1).map((id) => quote(id) && <blockquote key={id}>{quote(id)}</blockquote>)
                )}
                {s.context?.map((c) => (
                  <p key={c} className="context">
                    {c}
                  </p>
                ))}
                <details>
                  <summary>{isOpen(s) ? 'What to check' : 'Details'}</summary>
                  <p>{s.summary}</p>
                  {s.sources.slice(isOpen(s) ? 1 : 0).map((id) => quote(id) && <blockquote key={id}>{quote(id)}</blockquote>)}
                  <p>{s.check}</p>
                </details>
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
            </li>
          ))}
          {unseen.map((kind) => (
            <li key={kind} className="unseen">
              <span className="result none" aria-label="Not seen" title="Not seen">
                –
              </span>
              <div>
                <strong>{LABEL[kind]}</strong>
                <p>Not seen in the chat</p>
              </div>
            </li>
          ))}
        </ul>
      )}
      {earlier.length > 0 && (
        <details className="earlier-checks">
          <summary>From earlier orders ({earlier.length})</summary>
          <ul className="checks">
            {earlier.map((s) => (
              <li key={s.key}>
                <span className={`result ${s.resolved || s.reviewed ? 'pass' : 'none'}`} aria-hidden="true">
                  {s.resolved || s.reviewed ? '✓' : '–'}
                </span>
                <div>
                  <strong>{LABEL[s.kind] ?? s.kind}</strong>
                  {s.sources.slice(0, 1).map((id) => quote(id) && <blockquote key={id}>{quote(id)}</blockquote>)}
                  <p>
                    {s.resolved
                      ? `Settled then: ${s.resolved}.`
                      : s.reviewed
                        ? `Reviewed by ${s.reviewed.by}: ${s.reviewed.note}`
                        : 'Not settled on that order.'}
                  </p>
                </div>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  )
}
