import { useEffect, useState } from 'react'
import { load } from '../lib/firebase'
import { mapsConfig, parsePin, pinText } from '../lib/maps'
import PinMap from '../orders/PinMap'

type Saved = { address: string; lat: number; lng: number; name: string; phone: string; updated_by: string; updated_at: string } | null

type Found = { lat: number; lng: number; formatted: string; precision_text: string; warning: string | null }

/** Where couriers pick up. Lalamove needs a map pin: found from the address and dragged into place, or pasted from Google Maps. */
export default function DispatchSettings({ readOnly = false }: { readOnly?: boolean }) {
  const [saved, setSaved] = useState<Saved>()
  const [form, setForm] = useState({ address: '', pin: '', name: '', phone: '' })
  const [found, setFound] = useState<Found>()
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const here = parsePin(form.pin)

  const locate = () => {
    setBusy(true)
    setError('')
    load<Found>('/settings/dispatch/locate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: form.address }),
    })
      .then(
        (f) => (setFound(f), setForm((x) => ({ ...x, pin: pinText(f.lat, f.lng) }))),
        (e) => setError(e.message),
      )
      .finally(() => setBusy(false))
  }

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
  const field = (k: keyof typeof form) => ({
    value: form[k],
    onChange: (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: e.target.value }),
  })

  return (
    <section className="order">
      <h2>Pickup for deliveries</h2>
      <p>
        Lalamove picks up here.{' '}
        {mapsConfig()
          ? 'Find your address on the map, then drag the pin to your door.'
          : 'In Google Maps, press and hold your pickup spot and copy the numbers it shows.'}
      </p>
      <form onSubmit={save}>
        <label>
          Pickup address
          <input required minLength={5} maxLength={300} {...field('address')} />
        </label>
        {mapsConfig() && !readOnly && (
          <p>
            <button type="button" disabled={busy || form.address.trim().length < 5} onClick={locate}>
              Find on map
            </button>
          </p>
        )}
        {found && (
          <p className={found.warning ? 'claim' : undefined}>
            <small>
              Google Maps found: {found.formatted} ({found.precision_text}). {found.warning && <strong>{found.warning}</strong>}
            </small>
          </p>
        )}
        {here && (
          <PinMap
            lat={here.lat}
            lng={here.lng}
            label="Pickup pin: drag it, or click the map"
            onMove={readOnly ? undefined : (lat, lng) => setForm((x) => ({ ...x, pin: pinText(lat, lng) }))}
          />
        )}
        <label>
          Pickup pin: or paste numbers or a Google Maps link
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
