import { useEffect, useState } from 'react'
import { api, load } from './firebase'
import type { Order } from './OrderView'

type Row = {
  line: number
  expected: { name: string; variant: string | null; quantity: number }
  observed: { counts: { variant: string | null; quantity: number }[]; damage: string | null; note: string } | null
  auto_result: 'matched' | 'conflict' | 'insufficient_visibility' | null
  manual: { by: string; at: string; reason: string } | null
  photos: string[]
}
export type PackingState = {
  version: number
  rows: Row[]
  photos: { id: string; sha256: string; uploaded_at: string; claimed_captured_at: string | null }[]
  suggestions: string[]
  checked_at?: string
  completed: { by: string; at: string } | null
}

const RESULT = {
  matched: 'Matches the order',
  conflict: 'Does not match',
  insufficient_visibility: 'Cannot tell from the photos',
}

/** Photos are only served to signed-in sellers, so they are fetched with the token and shown from memory. */
function Photo({ orderId, id, n }: { orderId: string; id: string; n: number }) {
  const [src, setSrc] = useState<string>()
  useEffect(() => {
    let url: string | undefined
    api(`/orders/${orderId}/packing/photos/${id}`)
      .then((r) => (r.ok ? r.blob() : Promise.reject()))
      .then((b) => setSrc((url = URL.createObjectURL(b))), () => {})
    return () => {
      if (url) URL.revokeObjectURL(url)
    }
  }, [orderId, id])
  return (
    <figure>
      {src ? <img src={src} alt={`Packing photo ${n}`} /> : <div className="placeholder">Loading…</div>}
      <figcaption>Photo {n}</figcaption>
    </figure>
  )
}

export default function Packing({ order, onChange }: { order: Order; onChange: (o: Order) => void }) {
  const packing = order.packing
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [inspecting, setInspecting] = useState<number>()
  const [reason, setReason] = useState('')

  if (!packing) return order.status === 'confirmed' && order.packing_blocked ? <p className="packing">Packing: {order.packing_blocked}.</p> : null

  const run = (label: string, path: string, init: RequestInit = { method: 'POST' }) => {
    setBusy(label)
    setError('')
    return load<Order>(`/orders/${order.id}/packing${path}`, init)
      .then((o) => (onChange(o), true))
      .catch((e) => (setError(e.message), false))
      .finally(() => setBusy(''))
  }

  const upload = async (files: FileList | null) => {
    for (const file of Array.from(files ?? [])) {
      const ok = await run(`Uploading ${file.name}…`, '/photos', {
        method: 'POST',
        body: file,
        // What the device says about when the photo was taken. Stored as a claim, apart from the upload time.
        headers: { 'Content-Type': file.type, 'X-Claimed-Captured-At': new Date(file.lastModified).toISOString() },
      })
      if (!ok) break // stop at the first failed upload so the error stays visible
    }
  }

  const photoNumber = (id: string) => packing.photos.findIndex((p) => p.id === id) + 1
  const ok = (r: Row) => r.manual || r.auto_result === 'matched'
  const ready = packing.photos.length > 0 && packing.rows.every(ok)
  const stale = packing.checked_at && packing.photos.some((p) => p.uploaded_at > packing.checked_at!)
  const done = !!packing.completed

  return (
    <section className="packing">
      <h3>Packing{done && ' · packed'}</h3>
      {done && (
        <p role="status">
          Marked packed by {packing.completed!.by} on {new Date(packing.completed!.at).toLocaleString()}.
        </p>
      )}
      {!done && <p>Lay all items out so they can be counted, then take one overview photo. Add close-ups for anything the check cannot see.</p>}

      {packing.photos.length > 0 && (
        <div className="photos">
          {packing.photos.map((p, n) => (
            <Photo key={p.id} orderId={order.id} id={p.id} n={n + 1} />
          ))}
        </div>
      )}
      {!done && (
        <label className="upload">
          Add packing photos
          <input type="file" accept="image/jpeg,image/png,image/webp,image/heic,image/heif" capture="environment" multiple disabled={!!busy} onChange={(e) => upload(e.target.files).then(() => (e.target.value = ''))} />
        </label>
      )}

      <table>
        <thead>
          <tr>
            <th>Expected</th>
            <th>Seen in photos</th>
            <th>Result</th>
          </tr>
        </thead>
        <tbody>
          {packing.rows.map((r) => (
            <tr key={r.line}>
              <td>
                {r.expected.quantity} × {r.expected.name}
                {r.expected.variant && ` (${r.expected.variant})`}
              </td>
              <td>
                {r.observed ? (
                  <>
                    {r.observed.counts.length ? r.observed.counts.map((c) => `${c.quantity} ${c.variant ?? 'unknown variant'}`).join(', ') : 'Could not count'}
                    {r.observed.damage && <strong> · Damage: {r.observed.damage}</strong>}
                    <br />
                    <small>
                      {r.observed.note}
                      {r.photos.length > 0 && ` (photo ${r.photos.map(photoNumber).join(', ')})`}
                    </small>
                  </>
                ) : (
                  <em>Not checked</em>
                )}
              </td>
              <td>
                <span className={`result ${r.auto_result ?? 'none'}`}>{r.auto_result ? RESULT[r.auto_result] : 'Not checked'}</span>
                {r.manual && (
                  <p>
                    <small>
                      <strong>Seller confirmed</strong> ({r.manual.by}): {r.manual.reason}
                    </small>
                  </p>
                )}
                {!done && !ok(r) && r.auto_result && inspecting !== r.line && (
                  <button onClick={() => (setInspecting(r.line), setReason(''))}>I inspected it…</button>
                )}
                {inspecting === r.line && (
                  <form
                    onSubmit={(e) => {
                      e.preventDefault()
                      run('Saving…', `/lines/${r.line}/confirm`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ reason }),
                      }).then((saved) => saved && setInspecting(undefined))
                    }}
                  >
                    <label>
                      What did you check or fix?
                      <input required autoFocus maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
                    </label>
                    <button disabled={!!busy || !reason.trim()}>Confirm</button>{' '}
                    <button type="button" onClick={() => setInspecting(undefined)}>
                      Cancel
                    </button>
                  </form>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {packing.suggestions.length > 0 && !done && (
        <ul>
          {packing.suggestions.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ul>
      )}
      {stale && !done && <p>New photos were added since the last check.</p>}
      {error && <p role="alert">{error}</p>}
      {busy && <p role="status">{busy}</p>}
      {!done && (
        <p className="actions">
          <button disabled={!!busy || packing.photos.length === 0} onClick={() => run('Checking photos… this takes about 15 seconds', '/check')}>
            {packing.checked_at ? 'Check photos again' : 'Check photos'}
          </button>
          <button disabled={!!busy || !ready} onClick={() => run('Saving…', '/complete')}>
            Mark packed
          </button>
        </p>
      )}
      <p>
        <small>
          The check only reports what is visible. It cannot see inside wrapping or prove what stays in the parcel after sealing. You decide when it is packed.
        </small>
      </p>
    </section>
  )
}
