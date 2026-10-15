import jsQR from 'jsqr'
import { useEffect, useState } from 'react'
import { load, localMobile } from './firebase'

type Saved = {
  name: string
  mobile: string
  qr: { masked_name: string; user_id: string | null; payload: string } | null
  updated_by: string
  updated_at: string
} | null

/** Read the QR code in a screenshot, in the browser. Only the decoded text is sent to the backend. */
async function decode(file: File): Promise<string | null> {
  const bitmap = await createImageBitmap(file)
  const canvas = document.createElement('canvas')
  canvas.width = bitmap.width
  canvas.height = bitmap.height
  const ctx = canvas.getContext('2d')!
  ctx.drawImage(bitmap, 0, 0)
  return jsQR(ctx.getImageData(0, 0, canvas.width, canvas.height).data, canvas.width, canvas.height)?.data ?? null
}

/** The account customers are told to pay for direct GCash. Receipts are checked against it. */
export default function GcashSettings() {
  const [saved, setSaved] = useState<Saved>()
  const [name, setName] = useState('')
  const [mobile, setMobile] = useState('')
  const [qr, setQr] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const show = (s: Saved) => {
    setSaved(s)
    setName(s?.name ?? '')
    setMobile(s ? localMobile(s.mobile) : '')
    setQr(s?.qr?.payload ?? null)
  }

  useEffect(() => {
    load<Saved>('/settings/gcash').then(show, (e) => setError(e.message))
  }, [])

  const pick = async (file: File | undefined) => {
    if (!file) return
    setError('')
    const text = await decode(file).catch(() => null)
    if (text) setQr(text)
    else setError('No QR code found in that image. Upload a clear screenshot of your GCash QR.')
  }

  const save = (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    load<Saved>('/settings/gcash', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, mobile, qr }) })
      .then(show, (e) => setError(e.message))
      .finally(() => setBusy(false))
  }

  if (saved === undefined) return error ? <p role="alert">{error}</p> : <p>Loading…</p>

  return (
    <section className="order">
      <h2>GCash account</h2>
      <p>
        Used when a customer pays by GCash directly. The name and number on their receipt must fit these. Enter the name exactly as registered in GCash, including any
        middle name.
      </p>
      <form onSubmit={save}>
        <label>
          Registered name
          <input required minLength={2} maxLength={100} value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label>
          GCash mobile number
          <input required inputMode="tel" maxLength={30} placeholder="0917 123 4567" value={mobile} onChange={(e) => setMobile(e.target.value)} />
        </label>
        <label>
          GCash QR (optional): a screenshot from GCash → Receive → Receive via QR
          <input type="file" accept="image/*" onChange={(e) => pick(e.target.files?.[0])} />
        </label>
        <p>
          <small>
            {qr
              ? qr === saved?.qr?.payload
                ? `QR saved: GCash shows your name as ${saved.qr.masked_name}. Receipts are compared with that exactly.`
                : 'QR read. Save to check it against your name.'
              : 'Without a QR, receipt names are compared with the name above, letter by letter where GCash shows them.'}
          </small>
        </p>
        <button disabled={busy}>Save</button>
      </form>
      {saved && (
        <p>
          <small>
            Last saved by {saved.updated_by} on {new Date(saved.updated_at).toLocaleString()}. Changing it does not affect requests already sent.
          </small>
        </p>
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  )
}
