import { useEffect, useState } from 'react'
import { load, peso, STATUS } from './firebase'
import OrderView, { type Order } from './OrderView'

export default function Orders() {
  const [list, setList] = useState<Order[]>()
  const [openId, setOpenId] = useState<string>()
  const [error, setError] = useState('')

  useEffect(() => {
    load<Order[]>('/orders').then(setList, (e) => setError(e.message))
  }, [])

  const open = list?.find((o) => o.id === openId)

  return (
    <section>
      <h2>Orders</h2>
      {error && <p role="alert">{error}</p>}
      {!list && !error && <p>Loading…</p>}
      {list?.length === 0 && <p>No orders yet. Open a conversation and create a draft order.</p>}
      <ul className="threads">
        {list?.map((o) => (
          <li key={o.id}>
            <button onClick={() => setOpenId(o.id)} aria-current={o.id === openId}>
              <strong>{o.draft.recipient_name.value ?? 'Unnamed customer'}</strong>
              <span>
                {STATUS[o.status]} · {o.draft.total === null ? 'no total yet' : peso(o.draft.total)} · version {o.version}
              </span>
              <time>{new Date(o.created_at).toLocaleString()}</time>
            </button>
          </li>
        ))}
      </ul>
      {open && <OrderView key={open.id} order={open} onChange={(o) => setList(list?.map((x) => (x.id === o.id ? o : x)))} />}
    </section>
  )
}
