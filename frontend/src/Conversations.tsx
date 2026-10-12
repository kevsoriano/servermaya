import { useEffect, useState } from 'react'
import { api } from './firebase'

type Conversation = { id: string; updated_time: string; snippet: string; customer: { name: string } | null }
type Message = { id: string; text: string; from_page: boolean; from_name: string; created_time: string; attachments: number }

async function load<T>(path: string): Promise<T> {
  const r = await api(path)
  if (!r.ok) throw new Error((await r.json().catch(() => null))?.detail ?? `Backend returned ${r.status}`)
  return r.json()
}

export default function Conversations() {
  const [list, setList] = useState<Conversation[]>()
  const [openId, setOpenId] = useState<string>()
  const [messages, setMessages] = useState<Message[]>()
  const [error, setError] = useState('')

  useEffect(() => {
    load<Conversation[]>('/conversations').then(setList, (e) => setError(e.message))
  }, [])

  const open = (id: string) => {
    setOpenId(id)
    setMessages(undefined)
    setError('')
    load<Message[]>(`/conversations/${id}`).then(setMessages, (e) => setError(e.message))
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
              {m.attachments > 0 && <em> [{m.attachments} attachment{m.attachments > 1 ? 's' : ''}]</em>}
            </p>
          </li>
        ))}
      </ol>
    </section>
  )
}
