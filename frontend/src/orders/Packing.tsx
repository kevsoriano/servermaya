import { useEffect, useState } from 'react'
import { api, load } from '../lib/firebase'
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
  /** Evidence of what was packed and sealed, for a later claim. The photo check counts from photos only. */
  videos?: { id: string; uploaded_at: string }[]
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
export function Photo({ path, alt, caption, video }: { path: string; alt: string; caption: string; video?: boolean }) {
  const [src, setSrc] = useState<string>()
  useEffect(() => {
    let url: string | undefined
    api(path)
      .then((r) => (r.ok ? r.blob() : Promise.reject()))
      .then(
        (b) => setSrc((url = URL.createObjectURL(b))),
        () => {},
      )
    return () => {
      if (url) URL.revokeObjectURL(url)
    }
  }, [path])
  return (
    <figure>
      {!src ? (
        <div className="placeholder">Loading…</div>
      ) : video ? (
        <video src={src} controls preload="metadata" aria-label={alt} />
      ) : (
        <img src={src} alt={alt} />
      )}
      <figcaption>{caption}</figcaption>
    </figure>
  )
}

/** The order code the customer sees on the chat's buttons; the claim review reads it off this label in a video. */
const orderCode = (id: string) => id.slice(0, 5).toUpperCase()

const esc = (s: string | null) => (s ?? '').replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`)

/** A printable packing label. Names and addresses come from the customer's chat, so every value is escaped. */
function printLabel(order: Order) {
  const w = window.open('', '_blank', 'width=480,height=640')
  if (!w) return
  const d = order.draft
  const items = d.items.map((i) => `${i.quantity} × ${esc(i.name)}${i.variant ? ` (${esc(i.variant)})` : ''}`).join('<br>')
  w.document.write(`<!doctype html><meta charset="utf-8"><title>Label #${orderCode(order.id)}</title>
<style>body{font:16px system-ui,sans-serif;margin:24px;color:#000}.l{border:2px solid #000;padding:20px;max-width:360px}
small{letter-spacing:.1em}h1{font-size:56px;margin:0 0 12px;letter-spacing:.04em}p{margin:8px 0}</style>
<div class="l"><small>ORDER</small><h1>#${orderCode(order.id)}</h1>
<p><b>To: ${esc(d.recipient_name.value)}</b><br>${esc(d.phone.value)}<br>${esc(d.address.value)}</p><p>${items}</p></div>`)
  w.document.close()
  w.focus()
  w.print()
}

export default function Packing({ order, onChange }: { order: Order; onChange: (o: Order) => void }) {
  const packing = order.packing
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [inspecting, setInspecting] = useState<number>()
  const [reason, setReason] = useState('')

  if (!packing)
    return order.status === 'confirmed' && order.packing_blocked ? <p className="packing">Packing: {order.packing_blocked}.</p> : null

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
      {order.manual_payment?.status === 'seller_confirmed' &&
        order.manual_payment.version === order.version &&
        order.payment?.status !== 'verified' && (
          <p>
            <small>
              Paid by direct GCash, confirmed by {order.manual_payment.confirmed?.by} in their GCash history. Not verified by PayMongo.
            </small>
          </p>
        )}
      {done && (
        <p role="status">
          Marked packed by {packing.completed!.by} on {new Date(packing.completed!.at).toLocaleString()}.
        </p>
      )}
      {!done && (
        <>
          <p>Lay all items out so they can be counted, then take one overview photo. Add close-ups for anything the check cannot see.</p>
          <p>
            Stick the packing label on the box. If you film packing and sealing, end the video on the label: a claim's unboxing video is
            checked against it.{' '}
            <button type="button" onClick={() => printLabel(order)}>
              Print packing label #{orderCode(order.id)}
            </button>
          </p>
        </>
      )}

      {(packing.photos.length > 0 || !!packing.videos?.length) && (
        <div className="photos">
          {packing.photos.map((p, n) => (
            <Photo
              key={p.id}
              path={`/orders/${order.id}/packing/photos/${p.id}`}
              alt={`Packing photo ${n + 1}`}
              caption={`Photo ${n + 1}`}
            />
          ))}
          {packing.videos?.map((v, n) => (
            <Photo
              key={v.id}
              video
              path={`/orders/${order.id}/packing/photos/${v.id}`}
              alt={`Packing video ${n + 1}`}
              caption={`Packing video ${n + 1}`}
            />
          ))}
        </div>
      )}
      {!done && (
        <label className="upload">
          Add packing photos, or a video of packing and sealing it
          <input
            type="file"
            accept="image/jpeg,image/png,image/webp,image/heic,image/heif,video/mp4,video/quicktime,video/webm"
            capture="environment"
            multiple
            disabled={!!busy}
            onChange={(e) => upload(e.target.files).then(() => (e.target.value = ''))}
          />
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
                    {r.observed.counts.length
                      ? r.observed.counts.map((c) => `${c.quantity} ${c.variant ?? 'unknown variant'}`).join(', ')
                      : 'Could not count'}
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
          <button
            disabled={!!busy || packing.photos.length === 0}
            onClick={() => run('Checking photos… this takes about 15 seconds', '/check')}
          >
            {packing.checked_at ? 'Check photos again' : 'Check photos'}
          </button>
          <button disabled={!!busy || !ready} onClick={() => run('Saving…', '/complete')}>
            Mark packed
          </button>
        </p>
      )}
      <p>
        <small>
          The check only reports what is visible. It cannot see inside wrapping or prove what stays in the parcel after sealing. You decide
          when it is packed.
        </small>
      </p>
    </section>
  )
}
