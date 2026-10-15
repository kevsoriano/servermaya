import { useState } from 'react'
import { load } from './firebase'

type Props = {
  conversationId: string
  text: string
  setText: (t: string) => void
  orderId?: string
  /** When the 24-hour reply window closes. Unknown means the backend decides. */
  until?: Date | null
  onSent?: () => void
}

/** Sends one message as the Page. Nothing is sent without the seller pressing Send. */
export default function Reply({ conversationId, text, setText, orderId, until, onSent }: Props) {
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState('')
  const [openedAt] = useState(() => Date.now())
  const closed = until !== undefined && (!until || until.getTime() < openedAt)

  const send = (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setStatus('')
    load(`/conversations/${conversationId}/reply`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text.trim(), order_id: orderId ?? null }),
    })
      .then(() => {
        setText('')
        setStatus('Sent.')
        onSent?.()
      })
      .catch((e) => setStatus(e.message))
      .finally(() => setBusy(false))
  }

  return (
    <form className="reply" onSubmit={send}>
      <textarea
        aria-label="Message to the customer on Messenger"
        placeholder={closed ? 'Reply window closed' : 'Reply in Messenger…'}
        value={text}
        onChange={(e) => setText(e.target.value)}
        maxLength={2000}
        rows={2}
        disabled={closed}
      />
      <div className="reply-foot">
        <small>
          {closed
            ? <>Messenger only allows replies within 24 hours of the customer's last message{until && <> (closed {until.toLocaleString()})</>}.</>
            : until && <>You can reply until {until.toLocaleString()}.</>}
        </small>
        {status && <span role="status">{status}</span>}
        <button className="primary" disabled={busy || closed || !text.trim()}>{busy ? 'Sending…' : 'Send'}</button>
      </div>
    </form>
  )
}
