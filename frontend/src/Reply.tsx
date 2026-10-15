import { useState } from 'react'
import { load } from './firebase'

const IMAGE_TYPES = 'image/jpeg,image/png,image/gif' // what Messenger shows as an image

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
  const [image, setImage] = useState<File>()
  const [picker, setPicker] = useState(0) // remounts the file input so the same file can be picked again
  const [openedAt] = useState(() => Date.now())
  const closed = until !== undefined && (!until || until.getTime() < openedAt)

  const clearImage = () => (setImage(undefined), setPicker((n) => n + 1))

  // Text and image go as two Messenger messages: the text first, then the image.
  const send = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setStatus('')
    const path = `/conversations/${conversationId}/reply`
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
    <form className="reply" onSubmit={submit}>
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
            <input key={picker} type="file" accept={IMAGE_TYPES} disabled={closed} hidden onChange={(e) => setImage(e.target.files?.[0])} />
          </label>
        )}
        <button className="primary" disabled={busy || closed || (!text.trim() && !image)}>{busy ? 'Sending…' : 'Send'}</button>
      </div>
    </form>
  )
}
