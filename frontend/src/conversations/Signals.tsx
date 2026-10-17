import { useEffect, useRef, useState } from 'react'
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
  /** Changes when a new message arrives or a payment may settle a check, so the checks are refreshed. */
  latest?: string
  /** Told how many checks are open, so the pane can say so at the top. */
  onOpenCount?: (n: number) => void
}

/** What to verify before shipping, each tied to the messages it came from. Never a verdict on the customer. */
const ABOUT =
  'AI reading of what was said, to help you decide what to verify. "Not seen" means the AI did not spot it, not that it was checked. Never a judgement of the customer, and it never blocks an order.'

export default function Signals({ conversationId, messages, latest, onOpenCount }: Props) {
  // Kept with the chat they are about, so another chat's checks never show here, nor their count above.
  const [loaded, setLoaded] = useState<{ id: string; s: Signal[] }>()
  const [error, setError] = useState('')
  const [tries, setTries] = useState(0) // Try again: checks that could not be read are never kept, so asking again retries

  // One request at a time per chat: a refresh asked for while one is running waits for it, and by then the analysis is
  // cached, so overlapping refreshes never start a second Gemini call. A refresh superseded while waiting is skipped.
  const running = useRef<{ id: string; done: Promise<unknown> }>(undefined)
  useEffect(() => {
    let live = true
    const before = running.current?.id === conversationId ? running.current.done : Promise.resolve()
    const done = before.then(
      () =>
        live &&
        load<Signal[]>(`/conversations/${conversationId}/signals`).then(
          (s) => live && (setLoaded({ id: conversationId, s }), setError('')),
          (e) => live && setError(e.message),
        ),
    )
    running.current = { id: conversationId, done }
    return () => {
      live = false
    }
  }, [conversationId, latest, tries])

  const signals = loaded?.id === conversationId ? loaded.s : undefined
  const isOpen = (s: Signal) => !s.resolved && !s.reviewed && !s.earlier
  const current = signals?.filter((s) => !s.earlier)
  const earlier = signals?.filter((s) => s.earlier) ?? []
  const open = signals?.filter(isOpen) ?? []
  useEffect(() => onOpenCount?.(signals ? open.length : 0), [signals, open.length, onOpenCount])
  // Every kind is listed, so the seller sees what was looked for. A kind with nothing found is "not seen", never a pass:
  // the AI missing something is not the same as code checking it.
  const found = current && [...open, ...current.filter((s) => !isOpen(s))]
  const unseen = Object.keys(LABEL).filter((kind) => !current?.some((s) => s.kind === kind))

  const quote = (id: string) => messages.find((m) => m.id === id)?.text

  // A message two checks rest on is quoted once, in the first of them.
  const quoted = new Set<string>()
  const firstQuote = (s: Signal) => {
    const id = s.sources.find((x) => quote(x))
    if (!id) return null
    if (quoted.has(id)) return <p className="same-quote">Same message as above</p>
    quoted.add(id)
    return <blockquote>{quote(id)}</blockquote>
  }

  return (
    <section className="signals" id="checks" aria-label="Checks before shipping">
      <h3>
        Checks before shipping{' '}
        <span className="info" tabIndex={0} role="img" aria-label={ABOUT} data-tip={ABOUT}>
          ⓘ
        </span>
        {signals && <span className={`pill ${open.length ? 'warn' : ''}`}>{open.length ? `${open.length} to verify` : 'None open'}</span>}
      </h3>
      {error && (
        <p role="alert">
          {error} <button onClick={() => (setError(''), setTries((n) => n + 1))}>Try again</button>
        </p>
      )}
      {!signals && !error && <p>Reading the conversation…</p>}
      {found && found.length > 0 && (
        <ul className="checks">
          {found.map((s) => (
            <li key={s.key} className={isOpen(s) ? 'to-verify' : 'done'}>
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
                    Reviewed by {s.reviewed.by}: {s.reviewed.note}
                  </p>
                ) : (
                  <>
                    {firstQuote(s)}
                    <p className="todo">
                      <span>What to do</span> {s.check}
                    </p>
                  </>
                )}
                {!!s.context?.length && (
                  <div className="records">
                    <span>From your records</span>
                    <ul>
                      {s.context.map((c) => (
                        <li key={c}>{c}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {isOpen(s) && (
                  <details>
                    <summary>Why it was flagged</summary>
                    <p>{s.summary}</p>
                    {s.sources.slice(1).map((id) => quote(id) && <blockquote key={id}>{quote(id)}</blockquote>)}
                  </details>
                )}
                {!isOpen(s) && (
                  <details>
                    <summary>Details</summary>
                    <p>{s.summary}</p>
                    {s.sources.map((id) => quote(id) && <blockquote key={id}>{quote(id)}</blockquote>)}
                    <p>{s.check}</p>
                  </details>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
      {current && unseen.length > 0 && (
        <div className="unseen">
          <span>Not seen in chat</span>
          <ul>
            {unseen.map((kind) => (
              <li key={kind}>{LABEL[kind]}</li>
            ))}
          </ul>
        </div>
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
