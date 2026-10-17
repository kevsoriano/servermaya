import { useState } from 'react'
import { load } from '../lib/firebase'

const IMAGE_TYPES = 'image/jpeg,image/png,image/gif' // what Messenger shows as an image
// Demo chats also take a video, such as a packing video
const DEMO_IMAGE_TYPES = `${IMAGE_TYPES},image/webp,video/mp4,video/quicktime,video/webm`

type Props = {
  conversationId: string
  text: string
  setText: (t: string) => void
  orderId?: string
  /** When the 24-hour reply window closes. Unknown means the backend decides. */
  until?: Date | null
  onSent?: () => void
}

/** Sends one message as the Page. Nothing is sent without the seller pressing Send. In a demo chat the message is only stored. */
export default function Reply({ conversationId, text, setText, orderId, until, onSent }: Props) {
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
        aria-label={demo ? 'Message to the customer' : 'Message to the customer on Messenger'}
        placeholder={demo ? 'Reply as the seller…' : closed ? 'Reply window closed' : 'Reply in Messenger…'}
        value={text}
        onChange={(e) => setText(e.target.value)}
        maxLength={2000}
        rows={2}
        disabled={closed}
      />
      <div className="reply-foot">
        <small>
          {demo ? (
            'Not sent to Messenger. The customer replies on their own.'
          ) : closed ? (
            <>Messenger's window to reply after the customer's last message has closed{until && <> ({until.toLocaleString()})</>}.</>
          ) : (
            until && <>You can reply until {until.toLocaleString()}.</>
          )}
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
          <label className="attach" title="Attach image">
            <svg
              aria-hidden="true"
              width="20"
              height="20"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48" />
            </svg>
            <span className="sr-only">{demo ? 'Attach a photo or video' : 'Attach image'}</span>
            <input
              key={picker}
              type="file"
              accept={demo ? DEMO_IMAGE_TYPES : IMAGE_TYPES}
              disabled={closed}
              hidden
              onChange={(e) => setImage(e.target.files?.[0])}
            />
          </label>
        )}
        <button className="primary" disabled={busy || closed || (!text.trim() && !image)}>
          {busy ? 'Sending…' : 'Send'}
        </button>
      </div>
    </form>
  )
}
