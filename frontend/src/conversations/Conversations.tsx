import { useEffect, useState, type CSSProperties } from 'react'
import { load } from '../lib/firebase'
import { peso, STATUS } from '../lib/format'
import type { Order } from '../orders/OrderView'
import VerifyPanel from '../orders/VerifyPanel'
import CustomerBrief from './CustomerBrief'
import Reply from './Reply'
import ScreenshotView from './ScreenshotView'
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
type Message = {
  id: string
  text: string
  from_page: boolean
  from_name: string
  created_time: string
  attachments: Attachment[]
  /** Demo chats: the quick-reply button under the seller's message, as Messenger shows it. */
  quick_replies?: string[]
}
type Attachment = { kind: 'image' | 'video' | 'audio' | 'file'; name: string | null; url: string; preview_url: string | null }

// Links come from Meta and expire, so an old thread may show broken media until it is reloaded.
// Images open in the viewer, which shows a GCash screenshot's checks next to it.
function Media({ a, onOpen }: { a: Attachment; onOpen: () => void }) {
  if (a.kind === 'image')
    return (
      <button type="button" className="media-open" onClick={onOpen} aria-label={`Enlarge ${a.name ?? 'image'}`}>
        <img src={a.preview_url ?? a.url} alt={a.name ?? 'Image from the conversation'} loading="lazy" />
      </button>
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

function replyUntil(messages: Message[], hours: number) {
  const last = Math.max(...messages.filter((m) => !m.from_page).map((m) => Date.parse(m.created_time)))
  return Number.isFinite(last) ? new Date(last + hours * 3600 * 1000) : null
}

const HOUR = 3600 * 1000

/** Links in a message open in a new tab, such as the payment link the seller sent. */
function linked(text: string) {
  return text.split(/(https:\/\/\S+)/).map((part, n) =>
    n % 2 ? (
      <a key={n} href={part} target="_blank" rel="noreferrer">
        {part}
      </a>
    ) : (
      part
    ),
  )
}

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
      .then(
        ({ id }) => (setOpen(false), setName(''), setChat(''), onDone(id)),
        (e) => setError(e.message),
      )
      .finally(() => setBusy(false))
  }

  const reset = () => {
    if (
      !confirm(
        'Delete your demo chats and every order made from them, then load the starting demo chats again? Messenger chats and their orders are not touched.',
      )
    )
      return
    setBusy(true)
    setError('')
    load('/demo/reset', { method: 'POST' })
      .then(
        () => onDone(),
        (e) => setError(e.message),
      )
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
              placeholder={'Customer: Hi, how much is the mug?\nSeller: ₱180 each.'}
            />
          </label>
          <p className="actions">
            <button className="primary" disabled={busy}>
              Create
            </button>
            <button type="button" onClick={() => setOpen(false)}>
              Cancel
            </button>
          </p>
        </form>
      ) : (
        <p className="actions">
          <button onClick={() => setOpen(true)} disabled={busy}>
            New demo chat
          </button>
          <button onClick={reset} disabled={busy}>
            {busy ? 'Resetting…' : 'Reset demo chats'}
          </button>
        </p>
      )}
      {error && <p role="alert">{error}</p>}
    </div>
  )
}

export function Avatar({ name, src }: { name: string; src?: string | null }) {
  const [broken, setBroken] = useState(false) // Meta's photo links expire; fall back to initials
  if (src && !broken) return <img className="avatar" src={src} alt="" onError={() => setBroken(true)} />
  const initials = name
    .split(/\s+/)
    .map((w) => w[0])
    .slice(0, 2)
    .join('')
    .toUpperCase()
  // A steady hue per name, so each customer keeps the same color across the app.
  const hue = [...name].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7)
  return (
    <span className="avatar" aria-hidden="true" style={{ '--hue': hue } as CSSProperties}>
      {initials || '?'}
    </span>
  )
}

/** Time today, date otherwise, like an inbox list. */
const when = (iso: string) => {
  const d = new Date(iso)
  return d.toDateString() === new Date().toDateString()
    ? d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
    : d.toLocaleDateString()
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

const done = (o: Order) => o.status === 'cancelled' || o.receipt?.status === 'customer_confirmed'

export default function Conversations({ replyHours, onOpenOrder }: { replyHours: number; onOpenOrder: (id: string) => void }) {
  const [list, setList] = useState<Conversation[]>()
  const [openId, setOpenId] = useState<string>()
  const [messages, setMessages] = useState<Message[]>()
  const [error, setError] = useState('')
  const [order, setOrder] = useState<Order>()
  const [openChecks, setOpenChecks] = useState(0) // open checks in this chat, said at the top of the pane
  const [orders, setOrders] = useState<Order[]>() // this chat's orders, newest first
  const [viewing, setViewing] = useState<{ a: Attachment; m: Message }>()
  const [drafting, setDrafting] = useState(false)
  const [reply, setReply] = useState('')
  const [asCustomer, setAsCustomer] = useState('')

  useEffect(() => {
    load<Conversation[]>('/conversations').then(setList, (e) => setError(e.message))
  }, [])

  // Only Messenger changes on its own; demo chats change only when this page writes to them, and it reloads after each write.
  const hasMessenger = !!list?.some((c) => !c.demo)

  // ponytail: polls Meta every few seconds while the tab is visible; switch to the Messenger webhook if it hits rate limits
  useEffect(() => {
    if (!hasMessenger) return
    let live = true // a reply for the previous thread must not land in the next one
    const tick = () => {
      if (document.visibilityState !== 'visible') return
      load<Conversation[]>('/conversations').then(
        (l) => live && setList((old) => (same(old, l, (c) => c.id + c.updated_time) ? old : l)),
        () => {},
      )
      if (openId && !openId.startsWith('demo_'))
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
  }, [openId, hasMessenger])

  // Mark the open thread read when it loads and again whenever the customer sends something new while it is open.
  const lastFromCustomer = messages?.findLast((m) => !m.from_page)?.id

  // Something new from the customer may be a tap on Confirm or Received, which the backend applies when the orders load.
  useEffect(() => {
    if (!openId || !lastFromCustomer) return
    load<Order[]>(`/conversations/${openId}/orders`).then(
      (l) => (setOrders(l), setOrder((o) => l.find((x) => x.id === o?.id) ?? l.find((x) => !done(x)) ?? l[0])),
      () => {},
    )
  }, [openId, lastFromCustomer])
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
    setOrders(undefined)
    setViewing(undefined)
    setReply('')
    setAsCustomer('')
    setError('')
    load<Message[]>(`/conversations/${id}`).then(setMessages, (e) => setError(e.message))
    // Open the order still in progress; otherwise the newest, so a returning customer's history is one click away.
    load<Order[]>(`/conversations/${id}/orders`).then(
      (l) => (setOrders(l), setOrder(l.find((o) => !done(o)) ?? l[0])),
      (e) => setError(e.message),
    )
  }

  // Demo chats are not polled, so refresh the list after anything that changes its snippet or order badges.
  const refreshDemoList = () => openId?.startsWith('demo_') && load<Conversation[]>('/conversations').then(setList, () => {})

  // PayMongo's checkout usually runs in another tab, so coming back here is when a demo chat's badges may have changed.
  useEffect(() => {
    if (!openId?.startsWith('demo_')) return
    const onFocus = () => load<Conversation[]>('/conversations').then(setList, () => {})
    window.addEventListener('focus', onFocus)
    return () => window.removeEventListener('focus', onFocus)
  }, [openId])

  const reloadMessages = () => {
    load<Message[]>(`/conversations/${openId}`).then(setMessages, (e) => setError(e.message))
    refreshDemoList()
  }

  // Demo: tapping a quick reply sends its text as the customer, as Messenger does.
  const [tapping, setTapping] = useState(false)
  const tap = (text: string) => {
    setTapping(true)
    load(`/conversations/${openId}/customer`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    })
      .then(reloadMessages, (e) => setError(e.message))
      .finally(() => setTapping(false))
  }

  const orderChanged = (o: Order) => {
    setOrder(o)
    setOrders((l) => (l?.some((x) => x.id === o.id) ? l.map((x) => (x.id === o.id ? o : x)) : [o, ...(l ?? [])]))
    refreshDemoList()
  }

  // After a new demo chat, open it; after a reset, close the open demo chat, which no longer exists.
  const demoChanged = (id?: string) => {
    if (!id && openId?.startsWith('demo_')) {
      setOpenId(undefined)
      setMessages(undefined)
      setOrder(undefined)
    }
    load<Conversation[]>('/conversations').then(
      (l) => {
        setList(l)
        if (id) open(id)
      },
      (e) => setError(e.message),
    )
  }

  const createDraft = () => {
    if (
      openOrder &&
      !confirm(
        'Is this a separate order? It is drafted from the messages after the last order. If the customer is changing the open order, edit that order instead.',
      )
    )
      return
    setDrafting(true)
    setError('')
    load<Order>(`/conversations/${openId}/draft-order`, { method: 'POST' })
      .then(orderChanged, (e) => setError(e.message))
      .finally(() => setDrafting(false))
  }

  const thread = list?.find((c) => c.id === openId)
  const openOrder = orders?.find((o) => !done(o))
  // A new order reads only the messages after the last one any order was drafted from, so offer it once the customer wrote since.
  const lastDrafted = orders?.[0]?.messages.at(-1)?.created_time
  // Taps on an order's Confirm or Received button answer that order; they are not the start of a new one.
  const taps = new Set(orders?.flatMap((o) => [o.asked?.confirm?.text, o.asked?.received?.text]))
  // Only words count: an image alone, such as a payment receipt, is about the order already open, not a new one.
  const customerWroteSince = !!messages?.some(
    (m) =>
      !m.from_page && !!m.text.trim() && !taps.has(m.text.trim()) && (!lastDrafted || Date.parse(m.created_time) > Date.parse(lastDrafted)),
  )
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
              <button
                onClick={() => open(c.id)}
                aria-current={c.id === openId}
                className={c.unread && c.id !== openId ? 'unread' : undefined}
              >
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
                  {!!c.open_checks && (
                    <mark>
                      {c.open_checks} {c.open_checks === 1 ? 'check' : 'checks'} before shipping
                    </mark>
                  )}
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
            {messages?.map((m, k) => (
              <li key={m.id} className={m.from_page ? 'page' : 'customer'}>
                <small>
                  {m.from_name} · {new Date(m.created_time).toLocaleString()}
                </small>
                {m.text && <p>{linked(m.text)}</p>}
                {m.attachments.map((a, n) => (
                  <Media key={n} a={a} onOpen={() => setViewing({ a, m })} />
                ))}
                {/* Like Messenger, a quick reply is offered only until the next message. */}
                {k === messages.length - 1 &&
                  m.quick_replies?.map((q) => (
                    <button
                      key={q}
                      type="button"
                      className="quick-reply"
                      disabled={tapping}
                      onClick={() => tap(q)}
                      title="Tap as the customer"
                    >
                      {q}
                    </button>
                  ))}
              </li>
            ))}
          </ol>
        </div>
        {openId && messages && thread?.demo && (
          <div className="composers">
            <Reply
              conversationId={openId}
              text={asCustomer}
              setText={setAsCustomer}
              asCustomer
              gcashWaiting={order?.manual_payment?.status === 'requested' || order?.manual_payment?.status === 'claimed'}
              paid={order?.payment?.status === 'verified'}
              onSent={reloadMessages}
            />
            <Reply conversationId={openId} text={reply} setText={setReply} onSent={reloadMessages} />
          </div>
        )}
        {openId && messages && !thread?.demo && (
          <Reply conversationId={openId} text={reply} setText={setReply} until={replyUntil(messages, replyHours)} onSent={reloadMessages} />
        )}
      </section>
      {openId && messages && (
        <aside className="pane side">
          <CustomerBrief
            conversationId={openId}
            name={name}
            avatar={<Avatar name={name} src={customer?.picture} />}
            changed={
              // The brief is about finished orders only, so it is asked for again only when one finishes or its claim changes.
              orders &&
              orders
                .filter(done)
                .map((o) =>
                  [
                    o.id,
                    o.status,
                    o.receipt?.status,
                    (o.claims ?? []).map((c) => `${c.status}/${c.decision?.cause ?? ''}`).join(),
                    o.returns?.length,
                  ].join(':'),
                )
                .join()
            }
          />
          {openChecks > 0 && (
            <a
              className="checks-alert"
              href="#checks"
              onClick={(e) => (e.preventDefault(), document.getElementById('checks')?.scrollIntoView({ behavior: 'smooth' }))}
            >
              <span aria-hidden>!</span>
              <span>
                <strong>{openChecks} to verify before shipping</strong>
                <small>From what was said in this chat</small>
              </span>
              <span aria-hidden>↓</span>
            </a>
          )}
          {order && <VerifyPanel order={order} onChange={orderChanged} onSent={reloadMessages} onOpen={onOpenOrder} chat={messages} />}
          {messages.length > 0 && orders && (!orders.length || customerWroteSince || drafting) && (
            <div className="block">
              <button className={openOrder ? undefined : 'primary'} onClick={createDraft} disabled={drafting}>
                {drafting ? 'Reading the conversation…' : orders.length ? '+ New order from the newer messages' : '+ Create draft order'}
              </button>
            </div>
          )}
          {orders && orders.length > 1 && (
            <details className="block">
              <summary>Orders in this chat ({orders.length})</summary>
              <ul className="chat-orders">
                {orders.map((o) => (
                  <li key={o.id}>
                    <button
                      type="button"
                      className={o.id === order?.id ? 'selected' : undefined}
                      onClick={() => setOrder(o)}
                      aria-pressed={o.id === order?.id}
                    >
                      <span>
                        {o.draft.items.map((i) => `${i.quantity}× ${i.name}${i.variant ? ` (${i.variant})` : ''}`).join(', ') || 'No items'}
                      </span>
                      <small>
                        {new Date(o.created_at).toLocaleDateString()} · {o.draft.total === null ? 'no total' : peso(o.draft.total)} ·{' '}
                        {o.receipt?.status === 'customer_confirmed' ? 'Received' : STATUS[o.status]}
                      </small>
                    </button>
                  </li>
                ))}
              </ul>
            </details>
          )}
          {messages.length > 0 && (
            <Signals
              conversationId={openId}
              messages={messages}
              // A new message, or a payment or order state that can settle a check (no Gemini call: only the settling reruns).
              latest={[
                messages.at(-1)?.id,
                ...(orders ?? []).map((o) => [o.id, o.status, o.payment?.status, o.manual_payment?.status, o.receipt?.status].join(':')),
              ].join()}
              onOpenCount={setOpenChecks}
            />
          )}
        </aside>
      )}
      {viewing && (
        <ScreenshotView
          url={viewing.a.url}
          alt={viewing.a.name ?? 'Image from the conversation'}
          messageId={viewing.m.id}
          fromCustomer={!viewing.m.from_page}
          orders={orders ?? []}
          onOrderChanged={orderChanged}
          onClose={() => setViewing(undefined)}
        />
      )}
    </div>
  )
}
