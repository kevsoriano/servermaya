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
  payment: 'pending' | 'verified' | 'mismatched' | 'expired' | null
  test_mode: boolean
  gcash_confirmed: boolean
}

export default function Confirm({ token }: { token: string }) {
  const [order, setOrder] = useState<Summary>()
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  // Back from PayMongo: ask the backend to check with PayMongo. Being redirected here proves nothing by itself.
  const returned = new URLSearchParams(location.search).has('returned')

  useEffect(() => {
    const path = returned ? `/orders/${token}/check-payment` : `/orders/${token}`
    loadPublic<Summary>(path, { method: returned ? 'POST' : 'GET' }).then(setOrder, (e) => setError(e.message))
  }, [token, returned])

  const act = (path: string, then: (r: Summary & { checkout_url?: string }) => void) => {
    setBusy(true)
    setError('')
    loadPublic<Summary>(`/orders/${token}${path}`, { method: 'POST' })
      .then(then, (e) => setError(e.message))
      .finally(() => setBusy(false))
  }

  if (!order) return <main>{error ? <p role="alert">{error}</p> : 'Loading…'}</main>

  return (
    <main className="order">
      <h1>{order.status === 'confirmed' ? 'Your order' : 'Please check your order'}</h1>
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
      {error && <p role="alert">{error}</p>}
      {order.status !== 'confirmed' ? (
        <>
          <p>If anything is wrong, message the seller and do not confirm.</p>
          <button onClick={() => act('/confirm', setOrder)} disabled={busy}>
            {busy ? 'Confirming…' : 'Yes, this is correct'}
          </button>
        </>
      ) : order.payment === 'verified' || order.gcash_confirmed ? (
        <p role="status">
          <strong>Payment received{order.payment !== 'verified' && ' by GCash'}. Thank you!</strong> The seller will prepare your order.
        </p>
      ) : order.payment === 'mismatched' ? (
        <p role="status">We received a payment that does not match this order. The seller will contact you.</p>
      ) : (
        <>
          <p role="status">
            {order.payment === 'pending' && returned
              ? 'We have not received your payment yet. If you just paid, wait a moment and check again.'
              : 'Order confirmed. Pay to complete it.'}
          </p>
          <p className="actions">
            <button onClick={() => act('/pay', (r) => r.checkout_url && location.assign(r.checkout_url))} disabled={busy}>
              Pay {peso(order.total)} with QR Ph
            </button>
            {order.payment === 'pending' && (
              <button onClick={() => act('/check-payment', setOrder)} disabled={busy}>
                I have paid, check again
              </button>
            )}
          </p>
          {order.test_mode && <p><mark>TEST</mark> This is a test payment page. No real money moves.</p>}
        </>
      )}
    </main>
  )
}
