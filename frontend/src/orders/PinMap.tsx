import { useEffect, useRef, useState } from 'react'
import { loadMaps, mapsConfig } from '../lib/maps'

/** A map with one draggable pin. Moving the pin (or clicking the map) calls onMove; a new lat/lng from outside moves the pin.
 * placed: false shows the map around lat/lng with no pin yet, until a click places one.
 * Renders nothing when Google Maps is not set up, so the pasted pin field still works on its own. */
export default function PinMap({
  lat,
  lng,
  onMove,
  label,
  placed = true,
}: {
  lat: number
  lng: number
  onMove?: (lat: number, lng: number) => void
  label: string
  placed?: boolean
}) {
  const box = useRef<HTMLDivElement>(null)
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const parts = useRef<{ map: any; marker: any }>(undefined)
  const moved = useRef(onMove)
  moved.current = onMove
  const [error, setError] = useState('')
  const config = mapsConfig()

  useEffect(() => {
    if (!config || !box.current || parts.current) return
    let live = true
    loadMaps().then(
      (maps) => {
        if (!live || !box.current) return
        const map = new maps.Map(box.current, {
          center: { lat, lng },
          zoom: placed ? 17 : 14,
          mapId: config.map_id,
          streetViewControl: false,
        })
        const marker = new maps.marker.AdvancedMarkerElement({
          map: placed ? map : null,
          position: { lat, lng },
          gmpDraggable: !!moved.current,
          title: label,
        })
        const place = (p: { lat: () => number; lng: () => number }) => moved.current?.(p.lat(), p.lng())
        marker.addListener('dragend', () => {
          const p = marker.position
          moved.current?.(typeof p.lat === 'function' ? p.lat() : p.lat, typeof p.lng === 'function' ? p.lng() : p.lng)
        })
        map.addListener('click', (e: { latLng?: { lat: () => number; lng: () => number } }) => e.latLng && place(e.latLng))
        parts.current = { map, marker }
      },
      (e) => setError(e.message),
    )
    return () => {
      live = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [config])

  // A pin set from outside (the address, a pasted link, the customer's pin, a click) moves the marker and the map.
  useEffect(() => {
    const p = parts.current
    if (!p) return
    p.marker.position = { lat, lng }
    p.marker.map = placed ? p.map : null
    p.map.panTo({ lat, lng })
  }, [lat, lng, placed])

  if (!config) return null
  return (
    <>
      <div ref={box} className="pin-map" role="application" aria-label={label} />
      {error && <p role="alert">{error}</p>}
    </>
  )
}
