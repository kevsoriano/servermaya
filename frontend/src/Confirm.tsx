import { useEffect, useState } from 'react'
import { loadPublic, peso } from './firebase'

type Summary = {
  status: string
  items: { name: string; variant: string | null; quantity: number; unit_price: number | null }[]
  total: number | null
  recipient_name: string | null
  phone: string | null
  address: string | null
  delivery_instructions: string | null
}

export default function Confirm({ token }: { token: string }) {
  const [order, setOrder] = useState<Summary>()
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    loadPublic<Summary>(`/orders/${token}`).then(setOrder, (e) => setError(e.message))
  }, [token])

  const confirm = () => {
    setBusy(true)
    loadPublic<Summary>(`/orders/${token}/confirm`, { method: 'POST' })
      .then(setOrder, (e) => setError(e.message))
      .finally(() => setBusy(false))
  }

  if (error) return <main><p role="alert">{error}</p></main>
  if (!order) return <main>Loading…</main>

  return (
    <main className="order">
      <h1>Please check your order</h1>
      <table>
        <tbody>
          {order.items.map((i, n) => (
            <tr key={n}>
              <td>
                {i.quantity} × {i.name}
                {i.variant && ` (${i.variant})`}
              </td>
              <td>{peso(i.unit_price === null ? null : i.unit_price * i.quantity)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <th>Total</th>
            <th>{peso(order.total)}</th>
          </tr>
        </tfoot>
      </table>
      <dl>
        <dt>Deliver to</dt>
        <dd>{order.recipient_name}</dd>
        <dd>{order.phone}</dd>
        <dd>{order.address}</dd>
        {order.delivery_instructions && <dd>Note: {order.delivery_instructions}</dd>}
      </dl>
      {order.status === 'confirmed' ? (
        <p role="status">
          <strong>Order confirmed. Thank you!</strong> The seller will send payment details next.
        </p>
      ) : (
        <>
          <p>If anything is wrong, message the seller and do not confirm.</p>
          <button onClick={confirm} disabled={busy}>
            {busy ? 'Confirming…' : 'Yes, this is correct'}
          </button>
        </>
      )}
    </main>
  )
}
