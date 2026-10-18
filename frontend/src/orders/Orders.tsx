import { useEffect, useState } from 'react'
import { customerReply, load } from '../lib/firebase'
import { fromChat, peso, STATUS } from '../lib/format'
import OrderView, { type ChatMessage, type Order } from './OrderView'
import { Avatar } from '../conversations/Conversations'

/** Who the order's chat is with, when that is not the recipient (a gift, or ordering for someone else), so it can be found. */
function chatWith(o: Order) {
  const name = o.messages.find((m) => !m.from_page)?.from_name
  const recipient = o.draft.recipient_name.value
  return name && name.trim().toLowerCase() !== recipient?.trim().toLowerCase() ? name : null
}

/** The order workspace: every order, and the open one's stages beside its evidence. initial: the order to open, such as one
 * opened from its chat. */
export default function Orders({ initial }: { initial?: string }) {
  const [list, setList] = useState<Order[]>()
  const [openId, setOpenId] = useState(initial)
  const [error, setError] = useState('')
  const [chat, setChat] = useState<{ id: string; messages: ChatMessage[] }>()

  useEffect(() => {
    load<Order[]>('/orders').then(
      // The list holds the newest orders: an older one opened from its chat is fetched and added.
      (l) =>
        !initial || l.some((o) => o.id === initial)
          ? setList(l)
          : load<Order>(`/orders/${initial}`).then(
              (o) => setList([o, ...l]),
              () => setList(l),
            ),
      (e) => setError(e.message),
    )
  }, [initial])

  const open = list?.find((o) => o.id === openId)
  const conversation = open && fromChat(open) ? open.source.conversation_id : undefined
  // The live chat, so a complaint there can become a claim and a pin the customer sent can be used. Reloaded after a send.
  const [sends, setSends] = useState(0)
  // A demo customer who could not answer, for this order, so the seller can ask again instead of waiting for a reply
  const [replyError, setReplyError] = useState<{ order: string; message: string }>()
  const answer = (order: string, conversationId: string) => {
    setReplyError(undefined)
    customerReply(conversationId)
      .catch((e) => setReplyError({ order, message: e.message }))
      .finally(() => setSends((n) => n + 1))
  }
  useEffect(() => {
    if (!conversation) return
    load<ChatMessage[]>(`/conversations/${conversation}`).then(
      (m) => setChat({ id: conversation, messages: m }),
      () => setChat(undefined),
    )
  }, [conversation, sends])
  // The chat as far as the open order goes: a newer order's messages from the same chat are about that one.
  const newer = open && list?.filter((o) => o.source.conversation_id === conversation && o.created_at > open.created_at).at(-1)
  const cutoff = Date.parse(newer?.messages[0]?.created_time ?? '')
  const orderChat = chat?.id === conversation ? chat?.messages.filter((m) => !(Date.parse(m.created_time) >= cutoff)) : undefined

  return (
    <div className="inbox two">
      <aside className="pane">
        <h2 className="pane-title">Orders</h2>
        {error && <p role="alert">{error}</p>}
        {!list && !error && <p className="muted">Loading…</p>}
        {list?.length === 0 && <p className="muted">No orders yet. Open a conversation and create a draft order.</p>}
        <ul className="threads">
          {list?.map((o) => (
            <li key={o.id}>
              <button onClick={() => setOpenId(o.id)} aria-current={o.id === openId}>
                <Avatar name={o.draft.recipient_name.value ?? ''} />
                <div>
                  <div className="thread-head">
                    <strong>{o.draft.recipient_name.value ?? 'Unnamed customer'}</strong>
                    <time dateTime={o.created_at}>{new Date(o.created_at).toLocaleDateString()}</time>
                  </div>
                  {chatWith(o) && <span className="snippet">Chat with {chatWith(o)}</span>}
                  <span className="snippet">
                    {o.draft.total === null ? 'no total yet' : peso(o.draft.total)} · version {o.version}
                  </span>
                  <mark className={`status ${o.status}`}>{STATUS[o.status]}</mark>
                </div>
              </button>
            </li>
          ))}
        </ul>
      </aside>
      <section className="pane side wide">
        {open && conversation && replyError?.order === open.id && (
          <p className="turn-error" role="alert">
            {replyError.message}{' '}
            <button type="button" onClick={() => answer(open.id, conversation)}>
              Try again
            </button>
          </p>
        )}
        {open ? (
          <OrderView
            key={open.id}
            order={open}
            onChange={(o) => setList((l) => l?.map((x) => (x.id === o.id ? o : x)))}
            chat={orderChat}
            onSent={() => {
              setSends((n) => n + 1)
              // A demo customer answers here too; the chat reloads once they have.
              if (conversation?.startsWith('demo_')) answer(open.id, conversation)
            }}
          />
        ) : (
          <p className="empty">Pick an order to see it.</p>
        )}
      </section>
    </div>
  )
}
