import { useEffect, useState } from 'react'
import { load } from './firebase'

type Saved = { address: string; lat: number; lng: number; name: string; phone: string; updated_by: string; updated_at: string } | null

/** Where couriers pick up. Lalamove needs a map pin, which the seller pastes from Google Maps. */
export default function DispatchSettings() {
  const [saved, setSaved] = useState<Saved>()
  const [form, setForm] = useState({ address: '', pin: '', name: '', phone: '' })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const show = (s: Saved) => {
    setSaved(s)
    if (s) setForm({ address: s.address, pin: `${s.lat}, ${s.lng}`, name: s.name, phone: s.phone })
  }
  useEffect(() => {
    load<Saved>('/settings/dispatch').then(show, (e) => setError(e.message))
  }, [])

  const save = (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    load<Saved>('/settings/dispatch', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(form) })
      .then(show, (e) => setError(e.message))
      .finally(() => setBusy(false))
  }
  const field = (k: keyof typeof form) => ({ value: form[k], onChange: (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: e.target.value }) })

  return (
    <section className="order">
      <h2>Pickup for deliveries</h2>
      <p>Lalamove picks up here. In Google Maps, press and hold your pickup spot and copy the numbers it shows.</p>
      <form onSubmit={save}>
        <label>
          Pickup address
          <input required minLength={5} maxLength={300} {...field('address')} />
        </label>
        <label>
          Pickup pin
          <input required placeholder="14.5547, 121.0244" {...field('pin')} />
        </label>
        <label>
          Contact name for the driver
          <input required maxLength={100} {...field('name')} />
        </label>
        <label>
          Contact mobile number
          <input required inputMode="tel" placeholder="0917 123 4567" {...field('phone')} />
        </label>
        <button disabled={busy}>Save</button>
      </form>
      {saved?.updated_at && (
        <p>
          <small>
            Last saved by {saved.updated_by} on {new Date(saved.updated_at).toLocaleString()}.
          </small>
        </p>
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  )
}
