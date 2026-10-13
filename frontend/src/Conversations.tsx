import { useEffect, useState } from 'react'
import { load } from './firebase'
import OrderView, { type Order } from './OrderView'

type Conversation = { id: string; updated_time: string; snippet: string; customer: { name: string } | null }
type Message = { id: string; text: string; from_page: boolean; from_name: string; created_time: string; attachments: Attachment[] }
type Attachment = { kind: 'image' | 'video' | 'audio' | 'file'; name: string | null; url: string; preview_url: string | null }

// Links come from Meta and expire, so an old thread may show broken media until it is reloaded.
function Media({ a }: { a: Attachment }) {
  if (a.kind === 'image')
    return (
      <a href={a.url} target="_blank" rel="noreferrer">
        <img src={a.preview_url ?? a.url} alt={a.name ?? 'Image from the conversation'} loading="lazy" />
      </a>
    )
  if (a.kind === 'video') return <video src={a.url} poster={a.preview_url ?? undefined} controls preload="metadata" />
  if (a.kind === 'audio') return <audio src={a.url} controls preload="metadata" />
  return (
    <a href={a.url} target="_blank" rel="noreferrer">
      {a.name ?? 'Attached file'}
    </a>
  )
}

export default function Conversations() {
  const [list, setList] = useState<Conversation[]>()
  const [openId, setOpenId] = useState<string>()
  const [messages, setMessages] = useState<Message[]>()
  const [error, setError] = useState('')
  const [order, setOrder] = useState<Order>()
  const [drafting, setDrafting] = useState(false)

  useEffect(() => {
    load<Conversation[]>('/conversations').then(setList, (e) => setError(e.message))
  }, [])

  const open = (id: string) => {
    setOpenId(id)
    setMessages(undefined)
    setOrder(undefined)
    setError('')
    load<Message[]>(`/conversations/${id}`).then(setMessages, (e) => setError(e.message))
  }

  const createDraft = () => {
    setDrafting(true)
    setError('')
    load<Order>(`/conversations/${openId}/draft-order`, { method: 'POST' })
      .then(setOrder, (e) => setError(e.message))
      .finally(() => setDrafting(false))
  }

  return (
    <section>
      <h2>Messenger conversations</h2>
      {error && <p role="alert">{error}</p>}
      {!list && !error && <p>Loading…</p>}
      {list?.length === 0 && <p>No conversations yet.</p>}
      <ul className="threads">
        {list?.map((c) => (
          <li key={c.id}>
            <button onClick={() => open(c.id)} aria-current={c.id === openId}>
              <strong>{c.customer?.name ?? 'Unknown'}</strong>
              <span>{c.snippet}</span>
              <time>{new Date(c.updated_time).toLocaleString()}</time>
            </button>
          </li>
        ))}
      </ul>
      {openId && !messages && !error && <p>Loading messages…</p>}
      <ol className="messages">
        {messages?.map((m) => (
          <li key={m.id} className={m.from_page ? 'page' : 'customer'}>
            <small>
              {m.from_name} · {new Date(m.created_time).toLocaleString()}
            </small>
            <p>
              {m.text}
            </p>
            {m.attachments.map((a, n) => (
              <Media key={n} a={a} />
            ))}
          </li>
        ))}
      </ol>
      {messages && messages.length > 0 && !order && (
        <button onClick={createDraft} disabled={drafting}>
          {drafting ? 'Reading the conversation…' : 'Create draft order'}
        </button>
      )}
      {order && <OrderView order={order} onChange={setOrder} />}
    </section>
  )
}
