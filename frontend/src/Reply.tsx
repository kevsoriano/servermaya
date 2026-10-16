import { useState } from 'react'
import { load } from './firebase'

const IMAGE_TYPES = 'image/jpeg,image/png,image/gif' // what Messenger shows as an image
const DEMO_IMAGE_TYPES = `${IMAGE_TYPES},image/webp`

type Props = {
  conversationId: string
  text: string
  setText: (t: string) => void
  orderId?: string
  /** When the 24-hour reply window closes. Unknown means the backend decides. */
  until?: Date | null
  onSent?: () => void
  /** Demo chats only: write as the customer instead of the seller. */
  asCustomer?: boolean
}

/** Sends one message as the Page. Nothing is sent without the seller pressing Send. In a demo chat the message is only stored. */
export default function Reply({ conversationId, text, setText, orderId, until, onSent, asCustomer }: Props) {
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState('')
  const [image, setImage] = useState<File>()
  const [picker, setPicker] = useState(0) // remounts the file input so the same file can be picked again
  const [openedAt] = useState(() => Date.now())
  const demo = conversationId.startsWith('demo_')
  const closed = !demo && until !== undefined && (!until || until.getTime() < openedAt)

  const clearImage = () => (setImage(undefined), setPicker((n) => n + 1))

  // Text and image go as two Messenger messages: the text first, then the image.
  const send = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setStatus('')
    const path = `/conversations/${conversationId}/${asCustomer ? 'customer' : 'reply'}`
    if (text.trim()) {
      await load(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: text.trim(), order_id: orderId ?? null }),
      })
      setText('')
    }
    if (image) {
      await load(`${path}/image${orderId ? `?order_id=${encodeURIComponent(orderId)}` : ''}`, {
        method: 'POST',
        headers: { 'Content-Type': image.type },
        body: image,
      })
      clearImage()
    }
  }

  const submit = (e: React.FormEvent) =>
    send(e)
      .then(() => {
        setStatus('Sent.')
        onSent?.()
      })
      .catch((e) => setStatus(e.message))
      .finally(() => setBusy(false))

  return (
    <form className={asCustomer ? "reply as-customer" : "reply"} onSubmit={submit}>
      <textarea
        aria-label={asCustomer ? 'Message from the customer' : demo ? 'Message to the customer' : 'Message to the customer on Messenger'}
        placeholder={asCustomer ? 'Write as the customer…' : demo ? 'Reply as the seller…' : closed ? 'Reply window closed' : 'Reply in Messenger…'}
        value={text}
        onChange={(e) => setText(e.target.value)}
        maxLength={2000}
        rows={2}
        disabled={closed}
      />
      <div className="reply-foot">
        <small>
          {demo ? (asCustomer ? 'You play the customer in a demo chat.' : 'Demo chat: kept in this app, never sent to Messenger.') : closed
            ? <>Messenger only allows replies within 24 hours of the customer's last message{until && <> (closed {until.toLocaleString()})</>}.</>
            : until && <>You can reply until {until.toLocaleString()}.</>}
        </small>
        {status && <span role="status">{status}</span>}
        {image ? (
          <span>
            {image.name}{' '}
            <button type="button" onClick={clearImage} disabled={busy} aria-label={`Remove ${image.name}`}>
              ×
            </button>
          </span>
        ) : (
          <label className="attach">
            Attach image
            <input key={picker} type="file" accept={demo ? DEMO_IMAGE_TYPES : IMAGE_TYPES} disabled={closed} hidden onChange={(e) => setImage(e.target.files?.[0])} />
          </label>
        )}
        <button className="primary" disabled={busy || closed || (!text.trim() && !image)}>{busy ? 'Sending…' : 'Send'}</button>
      </div>
    </form>
  )
}
