import { useEffect, useState } from 'react'
import { load } from './firebase'
import OrderView, { type Order } from './OrderView'
import Reply from './Reply'
import Signals from './Signals'

type Conversation = {
  id: string
  updated_time: string
  snippet: string
  customer: { name: string; picture?: string | null } | null
  open_checks: number | null
  unread: boolean
  awaiting_reply: boolean
  reply_until: string | null
  order_actions: string[]
  demo?: boolean
}
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

function same<T>(a: T[] | undefined, b: T[], key: (x: T) => string) {
  return !!a && a.length === b.length && a.every((x, i) => key(x) === key(b[i]))
}

function replyUntil(messages: Message[]) {
  const last = Math.max(...messages.filter((m) => !m.from_page).map((m) => Date.parse(m.created_time)))
  return Number.isFinite(last) ? new Date(last + 24 * 3600 * 1000) : null
}

const HOUR = 3600 * 1000

/** Demo chats live in this app only, so judges can try every step without Messenger. */
function DemoTools({ onDone }: { onDone: (id?: string) => void }) {
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [chat, setChat] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const create = (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    load<{ id: string }>('/demo/threads', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name.trim() || 'Demo customer', chat }),
    })
      .then(({ id }) => (setOpen(false), setName(''), setChat(''), onDone(id)), (e) => setError(e.message))
      .finally(() => setBusy(false))
  }

  const reset = () => {
    if (!confirm('Delete your demo chats and every order made from them, then load the starting demo chats again? Messenger chats and their orders are not touched.')) return
    setBusy(true)
    setError('')
    load('/demo/reset', { method: 'POST' })
      .then(() => onDone(), (e) => setError(e.message))
      .finally(() => setBusy(false))
  }

  return (
    <div className="demo-tools">
      <p className="muted">
        <small>Demo chats stay in this app. You write both sides; nothing is sent to Messenger.</small>
      </p>
      {open ? (
        <form onSubmit={create}>
          <label>
            Customer name
            <input value={name} onChange={(e) => setName(e.target.value)} maxLength={100} placeholder="Demo customer" />
          </label>
          <label>
            Chat (optional): start each message with Customer: or Seller:
            <textarea
              rows={6}
              value={chat}
              onChange={(e) => setChat(e.target.value)}
              maxLength={50000}
              placeholder={'Customer: Hi, magkano po yung mug?\nSeller: ₱180 each po.'}
            />
          </label>
          <p className="actions">
            <button className="primary" disabled={busy}>Create</button>
            <button type="button" onClick={() => setOpen(false)}>Cancel</button>
          </p>
        </form>
      ) : (
        <p className="actions">
          <button onClick={() => setOpen(true)} disabled={busy}>New demo chat</button>
          <button onClick={reset} disabled={busy}>{busy ? 'Resetting…' : 'Reset demo chats'}</button>
        </p>
      )}
      {error && <p role="alert">{error}</p>}
    </div>
  )
}

export function Avatar({ name, src }: { name: string; src?: string | null }) {
  const [broken, setBroken] = useState(false) // Meta's photo links expire; fall back to initials
  if (src && !broken) return <img className="avatar" src={src} alt="" onError={() => setBroken(true)} />
  const initials = name.split(/\s+/).map((w) => w[0]).slice(0, 2).join('').toUpperCase()
  return <span className="avatar" aria-hidden="true">{initials || '?'}</span>
}

/** Time today, date otherwise, like an inbox list. */
const when = (iso: string) => {
  const d = new Date(iso)
  return d.toDateString() === new Date().toDateString() ? d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : d.toLocaleDateString()
}

/** What the seller still has to do on a thread. The open-checks badge is shown separately. */
function pending(c: Conversation) {
  const out: string[] = []
  if (c.awaiting_reply) {
    const left = c.reply_until ? Date.parse(c.reply_until) - Date.now() : 0
    out.push(left <= 0 ? 'Reply window closed' : left < 4 * HOUR ? `Reply within ${Math.ceil(left / HOUR)}h` : 'Needs a reply')
  }
  return [...out, ...c.order_actions]
}

export default function Conversations() {
  const [list, setList] = useState<Conversation[]>()
  const [openId, setOpenId] = useState<string>()
  const [messages, setMessages] = useState<Message[]>()
  const [error, setError] = useState('')
  const [order, setOrder] = useState<Order>()
  const [drafting, setDrafting] = useState(false)
  const [reply, setReply] = useState('')
  const [asCustomer, setAsCustomer] = useState('')

  useEffect(() => {
    load<Conversation[]>('/conversations').then(setList, (e) => setError(e.message))
  }, [])

  // ponytail: polls Meta every few seconds while the tab is visible; switch to the Messenger webhook if it hits rate limits
  useEffect(() => {
    let live = true // a reply for the previous thread must not land in the next one
    const tick = () => {
      if (document.visibilityState !== 'visible') return
      load<Conversation[]>('/conversations').then((l) => live && setList((old) => (same(old, l, (c) => c.id + c.updated_time) ? old : l)), () => {})
      if (openId)
        load<Message[]>(`/conversations/${openId}`).then(
          // Keep the old array when nothing new arrived, so playing audio or video is not reloaded with fresh Meta links.
          (m) => live && setMessages((old) => (old && same(old, m, (x) => x.id) ? old : m)),
          () => {},
        )
    }
    const timer = setInterval(tick, openId ? 5000 : 15000)
    return () => {
      live = false
      clearInterval(timer)
    }
  }, [openId])

  // Mark the open thread read when it loads and again whenever the customer sends something new while it is open.
  const lastFromCustomer = messages?.findLast((m) => !m.from_page)?.id
  useEffect(() => {
    if (!openId || !lastFromCustomer) return
    load(`/conversations/${openId}/seen`, { method: 'POST' }).then(
      () => setList((l) => l?.map((c) => (c.id === openId ? { ...c, unread: false } : c))),
      () => {},
    )
  }, [openId, lastFromCustomer])

  const open = (id: string) => {
    setOpenId(id)
    setMessages(undefined)
    setOrder(undefined)
    setReply('')
    setAsCustomer('')
    setError('')
    load<Message[]>(`/conversations/${id}`).then(setMessages, (e) => setError(e.message))
  }

  const reloadMessages = () => load<Message[]>(`/conversations/${openId}`).then(setMessages, (e) => setError(e.message))

  // After a new demo chat, open it; after a reset, close the open demo chat, which no longer exists.
  const demoChanged = (id?: string) => {
    if (!id && openId?.startsWith('demo_')) {
      setOpenId(undefined)
      setMessages(undefined)
      setOrder(undefined)
    }
    load<Conversation[]>('/conversations').then((l) => {
      setList(l)
      if (id) open(id)
    }, (e) => setError(e.message))
  }

  const createDraft = () => {
    setDrafting(true)
    setError('')
    load<Order>(`/conversations/${openId}/draft-order`, { method: 'POST' })
      .then(setOrder, (e) => setError(e.message))
      .finally(() => setDrafting(false))
  }

  const thread = list?.find((c) => c.id === openId)
  const customer = thread?.customer
  const name = customer?.name ?? 'Unknown'

  return (
    <div className="inbox">
      <aside className="pane">
        <h2 className="pane-title">Chats</h2>
        {!list && !error && <p className="muted">Loading…</p>}
        {list?.length === 0 && <p className="muted">No conversations yet.</p>}
        <ul className="threads">
          {list?.map((c) => (
            <li key={c.id}>
              <button onClick={() => open(c.id)} aria-current={c.id === openId} className={c.unread && c.id !== openId ? 'unread' : undefined}>
                <Avatar name={c.customer?.name ?? ''} src={c.customer?.picture} />
                <div>
                  <div className="thread-head">
                    <strong>
                      {c.unread && c.id !== openId && <span className="dot" role="img" aria-label="Unread" />}
                      {c.customer?.name ?? 'Unknown'}
                    </strong>
                    <time dateTime={c.updated_time} title={new Date(c.updated_time).toLocaleString()}>
                      {when(c.updated_time)}
                    </time>
                  </div>
                  <span className="snippet">{c.snippet}</span>
                  {c.demo && <mark className="source demo">Demo</mark>}
                  {!!c.open_checks && <mark>{c.open_checks} {c.open_checks === 1 ? 'check' : 'checks'} before shipping</mark>}
                  {pending(c).map((p) => (
                    <mark key={p} className="pending">
                      {p}
                    </mark>
                  ))}
                </div>
              </button>
            </li>
          ))}
        </ul>
        {list && <DemoTools onDone={demoChanged} />}
      </aside>
      <section className="pane chat">
        {openId && (
          <header className="chat-head">
            <Avatar name={name} src={customer?.picture} />
            <strong>{name}</strong>
          </header>
        )}
        {error && <p role="alert">{error}</p>}
        {!openId && !error && <p className="empty">Pick a conversation to read it.</p>}
        {openId && !messages && !error && <p className="empty">Loading messages…</p>}
        {/* column-reverse keeps the scroll pinned to the newest message without JS */}
        <div className="scroll">
          <ol className="messages">
            {messages?.map((m) => (
              <li key={m.id} className={m.from_page ? 'page' : 'customer'}>
                <small>
                  {m.from_name} · {new Date(m.created_time).toLocaleString()}
                </small>
                {m.text && <p>{m.text}</p>}
                {m.attachments.map((a, n) => (
                  <Media key={n} a={a} />
                ))}
              </li>
            ))}
          </ol>
        </div>
        {openId && messages && (
          <Reply
            conversationId={openId}
            text={reply}
            setText={setReply}
            until={thread?.demo ? undefined : replyUntil(messages)}
            onSent={reloadMessages}
          />
        )}
        {openId && messages && thread?.demo && (
          <Reply conversationId={openId} text={asCustomer} setText={setAsCustomer} asCustomer onSent={reloadMessages} />
        )}
      </section>
      {openId && messages && (
        <aside className="pane side">
          <div className="profile">
            <Avatar name={name} src={customer?.picture} />
            <strong>{name}</strong>
          </div>
          {messages.length > 0 && !order && (
            <div className="block">
              <h3>Order</h3>
              <button className="primary" onClick={createDraft} disabled={drafting}>
                {drafting ? 'Reading the conversation…' : '+ Create draft order'}
              </button>
            </div>
          )}
          {order && <OrderView order={order} onChange={setOrder} />}
          {messages.length > 0 && <Signals conversationId={openId} messages={messages} latest={messages.at(-1)?.id} />}
        </aside>
      )}
    </div>
  )
}
