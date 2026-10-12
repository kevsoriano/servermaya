import { useState } from 'react'
import { load, peso, STATUS } from './firebase'

type Sourced = { value: string | null; sources: string[] }
type Item = { name: string; variant: string | null; quantity: number; unit_price: number | null; sources: string[] }
export type Order = {
  id: string
  status: 'draft' | 'awaiting_confirmation' | 'confirmed' | 'cancelled'
  version: number
  created_at: string
  confirm_token: string | null
  confirmations: { version: number; kind: 'customer' | 'seller_asserted'; by: string; at: string }[]
  missing: string[]
  messages: { id: string; text: string; from_page: boolean }[]
  draft: {
    items: Item[]
    total: number | null
    recipient_name: Sourced
    phone: Sourced
    address: Sourced
    delivery_instructions: Sourced
    claims: { kind: string; text: string; reference: string | null; pressure: boolean; sources: string[] }[]
    clarification_questions: string[]
  }
}

const FIELDS = [
  ['recipient_name', 'Recipient'],
  ['phone', 'Phone'],
  ['address', 'Address'],
  ['delivery_instructions', 'Delivery instructions'],
] as const

type Form = { items: { name: string; variant: string; quantity: string; price: string }[] } & Record<(typeof FIELDS)[number][0], string>

const toForm = (d: Order['draft']): Form => ({
  items: d.items.map((i) => ({
    name: i.name,
    variant: i.variant ?? '',
    quantity: String(i.quantity),
    price: i.unit_price === null ? '' : String(i.unit_price / 100),
  })),
  recipient_name: d.recipient_name.value ?? '',
  phone: d.phone.value ?? '',
  address: d.address.value ?? '',
  delivery_instructions: d.delivery_instructions.value ?? '',
})

export default function OrderView({ order, onChange }: { order: Order; onChange: (o: Order) => void }) {
  const [form, setForm] = useState<Form>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)
  const { draft, messages } = order
  const confirmation = order.status === 'confirmed' ? order.confirmations.at(-1) : undefined
  const earlier = order.confirmations.filter((c) => c !== confirmation)
  const link = order.confirm_token && `${location.origin}/c/${order.confirm_token}`

  const call = (path: string, init: RequestInit) => {
    setBusy(true)
    setError('')
    return load<Order>(`/orders/${order.id}${path}`, init)
      .then((o) => {
        onChange(o)
        setForm(undefined)
        setCopied(false)
      })
      .catch((e) => setError(e.message))
      .finally(() => setBusy(false))
  }

  const save = (e: React.FormEvent) => {
    e.preventDefault()
    if (!form) return
    call('', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...form,
        items: form.items.map((i) => ({
          name: i.name.trim(),
          variant: i.variant.trim() || null,
          quantity: Number(i.quantity),
          unit_price: i.price === '' ? null : Math.round(Number(i.price) * 100),
        })),
      }),
    })
  }

  // Citations show as the message's position in the thread, with the text on hover.
  const src = (ids: string[]) =>
    ids.length === 0 ? (
      <sup title="Typed by the seller, not taken from the chat"> [seller]</sup>
    ) : (
      <sup>
        {ids.map((id) => {
          const n = messages.findIndex((m) => m.id === id)
          return (
            <abbr key={id} title={messages[n]?.text}>
              [{n + 1}]
            </abbr>
          )
        })}
      </sup>
    )

  const setItem = (n: number, patch: Partial<Form['items'][number]>) =>
    setForm((f) => f && { ...f, items: f.items.map((i, k) => (k === n ? { ...i, ...patch } : i)) })

  return (
    <section className="order">
      <h2>
        Order · version {order.version} · {STATUS[order.status]}
      </h2>

      {confirmation?.kind === 'customer' && (
        <p role="status">Customer confirmed version {confirmation.version} through the link on {new Date(confirmation.at).toLocaleString()}.</p>
      )}
      {confirmation?.kind === 'seller_asserted' && (
        <p role="status" className="claim">
          <strong>Not confirmed by the customer.</strong> {confirmation.by} recorded that the customer agreed to version {confirmation.version} on{' '}
          {new Date(confirmation.at).toLocaleString()}.
        </p>
      )}
      {earlier.length > 0 && (
        <p>
          <small>
            Earlier agreements: {earlier.map((c) => `version ${c.version} (${c.kind === 'customer' ? 'customer' : 'seller-reported'}, ${new Date(c.at).toLocaleDateString()})`).join('; ')}.
            {order.status !== 'confirmed' && ' The order changed since, so it needs confirming again.'}
          </small>
        </p>
      )}

      {form ? (
        <form onSubmit={save}>
          {form.items.map((i, n) => (
            <fieldset key={n}>
              <legend>Item {n + 1}</legend>
              <label>
                Name <input required value={i.name} onChange={(e) => setItem(n, { name: e.target.value })} />
              </label>
              <label>
                Variant <input value={i.variant} onChange={(e) => setItem(n, { variant: e.target.value })} />
              </label>
              <label>
                Quantity <input required type="number" min="1" step="1" value={i.quantity} onChange={(e) => setItem(n, { quantity: e.target.value })} />
              </label>
              <label>
                Unit price (₱) <input type="number" min="0" step="0.01" value={i.price} onChange={(e) => setItem(n, { price: e.target.value })} />
              </label>
              <button type="button" onClick={() => setForm({ ...form, items: form.items.filter((_, k) => k !== n) })}>
                Remove item
              </button>
            </fieldset>
          ))}
          <button type="button" onClick={() => setForm({ ...form, items: [...form.items, { name: '', variant: '', quantity: '1', price: '' }] })}>
            Add item
          </button>
          {FIELDS.map(([key, label]) => (
            <label key={key}>
              {label} <input value={form[key]} onChange={(e) => setForm({ ...form, [key]: e.target.value })} />
            </label>
          ))}
          {order.status !== 'draft' && <p>Saving a change creates a new version, and the customer will need to confirm again.</p>}
          <button disabled={busy}>{busy ? 'Saving…' : 'Save'}</button>{' '}
          <button type="button" onClick={() => setForm(undefined)}>
            Cancel
          </button>
        </form>
      ) : (
        <>
          <table>
            <thead>
              <tr>
                <th>Item</th>
                <th>Qty</th>
                <th>Unit price</th>
              </tr>
            </thead>
            <tbody>
              {draft.items.map((i, n) => (
                <tr key={n}>
                  <td>
                    {i.name}
                    {i.variant && ` (${i.variant})`}
                    {src(i.sources)}
                  </td>
                  <td>{i.quantity}</td>
                  <td>{peso(i.unit_price)}</td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <th colSpan={2}>Total</th>
                <td>{draft.total === null ? 'Unknown' : peso(draft.total)}</td>
              </tr>
            </tfoot>
          </table>
          <dl>
            {FIELDS.map(([key, label]) => (
              <div key={key}>
                <dt>{label}</dt>
                <dd>{draft[key].value ? <>{draft[key].value}{src(draft[key].sources)}</> : <em>Missing</em>}</dd>
              </div>
            ))}
          </dl>
          <p>
            <small>Numbers in brackets are the chat messages each value came from.</small>
          </p>
        </>
      )}

      {draft.claims.map((c, n) => (
        <p key={n} className="claim">
          <strong>Unverified {c.kind} claim.</strong> {c.text}
          {src(c.sources)}
          {c.reference && <> Reference: {c.reference}.</>}
          {c.pressure && <> The customer is asking you to act before this is verified.</>}
        </p>
      ))}
      {draft.clarification_questions.length > 0 && (
        <>
          <h3>Ask the customer</h3>
          <ul>
            {draft.clarification_questions.map((q) => (
              <li key={q}>{q}</li>
            ))}
          </ul>
        </>
      )}

      {error && <p role="alert">{error}</p>}
      {!form && order.status !== 'cancelled' && (
        <>
          {order.missing.length > 0 && <p>Before asking the customer to confirm, fill in: {order.missing.join(', ')}.</p>}
          {link && order.status === 'awaiting_confirmation' && (
            <p>
              Send this link to the customer:
              <br />
              <input readOnly value={link} onFocus={(e) => e.target.select()} aria-label="Confirmation link" />{' '}
              <button onClick={() => navigator.clipboard.writeText(link).then(() => setCopied(true))}>{copied ? 'Copied' : 'Copy'}</button>
            </p>
          )}
          <p className="actions">
            <button onClick={() => setForm(toForm(draft))}>Edit</button>
            {order.status === 'draft' && (
              <button disabled={busy || order.missing.length > 0} onClick={() => call('/send-confirmation', { method: 'POST' })}>
                Create confirmation link
              </button>
            )}
            {order.status !== 'confirmed' && (
              <button
                disabled={busy || order.missing.length > 0}
                onClick={() => confirm('Record that the customer agreed, without their own confirmation?') && call('/assert-confirmation', { method: 'POST' })}
              >
                Customer agreed another way
              </button>
            )}
          </p>
        </>
      )}
    </section>
  )
}
