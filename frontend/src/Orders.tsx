import { useEffect, useState } from 'react'
import { load, peso, STATUS } from './firebase'
import OrderView, { type Order } from './OrderView'
import { Avatar } from './Conversations'

export default function Orders() {
  const [list, setList] = useState<Order[]>()
  const [openId, setOpenId] = useState<string>()
  const [error, setError] = useState('')

  useEffect(() => {
    load<Order[]>('/orders').then(setList, (e) => setError(e.message))
  }, [])

  const open = list?.find((o) => o.id === openId)

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
        {open ? (
          <OrderView key={open.id} order={open} onChange={(o) => setList(list?.map((x) => (x.id === o.id ? o : x)))} />
        ) : (
          <p className="empty">Pick an order to see it.</p>
        )}
      </section>
    </div>
  )
}
