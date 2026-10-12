export type Sourced = { value: string | null; sources: string[] }
export type Order = {
  id: string
  status: string
  messages: { id: string; text: string; from_page: boolean }[]
  draft: {
    items: { name: string; variant: string | null; quantity: number; unit_price: number | null; sources: string[] }[]
    total: number | null
    recipient_name: Sourced
    phone: Sourced
    address: Sourced
    delivery_instructions: Sourced
    claims: { kind: string; text: string; amount: number | null; reference: string | null; pressure: boolean; status: string; sources: string[] }[]
    clarification_questions: string[]
  }
}

const peso = (centavos: number | null) =>
  centavos === null ? 'not stated' : (centavos / 100).toLocaleString('en-PH', { style: 'currency', currency: 'PHP' })

export default function Draft({ order }: { order: Order }) {
  const { draft, messages } = order
  // Citations show as the message's position in the thread, with the text on hover.
  const Src = ({ ids }: { ids: string[] }) => (
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
  const fields: [string, Sourced][] = [
    ['Recipient', draft.recipient_name],
    ['Phone', draft.phone],
    ['Address', draft.address],
    ['Delivery instructions', draft.delivery_instructions],
  ]

  return (
    <section className="draft">
      <h2>Draft order</h2>
      <p>
        <small>AI-extracted from the conversation. Not confirmed by the customer. Numbers in brackets are the source messages.</small>
      </p>
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
                <Src ids={i.sources} />
              </td>
              <td>{i.quantity}</td>
              <td>{peso(i.unit_price)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <th colSpan={2}>Total</th>
            <td>{draft.total === null ? 'Unknown: a price is missing' : peso(draft.total)}</td>
          </tr>
        </tfoot>
      </table>
      {draft.items.length === 0 && <p>No items found in this conversation.</p>}
      <dl>
        {fields.map(([label, f]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{f.value ? <>{f.value}<Src ids={f.sources} /></> : <em>Missing</em>}</dd>
          </div>
        ))}
      </dl>
      {draft.claims.length > 0 && <h3>Customer claims</h3>}
      {draft.claims.map((c, n) => (
        <p key={n} className="claim">
          <strong>Unverified {c.kind} claim.</strong> {c.text}
          <Src ids={c.sources} />
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
    </section>
  )
}
