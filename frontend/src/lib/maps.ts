/** Google Maps in the browser: the key and map ID come from /api/me (the key only works on this app's addresses). Without
 * them there is no map, and pins are pasted by hand as before. */
type MapsConfig = { key: string; map_id: string } | null

let config: MapsConfig = null
// ponytail: typed loosely instead of adding @types/google.maps for one draggable pin
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let loading: Promise<any> | undefined

export const setMapsConfig = (c: MapsConfig | undefined) => {
  config = c ?? null
}
export const mapsConfig = () => config

/** Loads the Maps JavaScript API once, with the marker library for a draggable pin. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function loadMaps(): Promise<any> {
  if (!config) return Promise.reject(new Error('Google Maps is not set up'))
  const { key } = config
  loading ??= new Promise((resolve, reject) => {
    const w = window as unknown as Record<string, unknown>
    w.__mapsReady = () => resolve((w.google as { maps: unknown }).maps)
    const s = document.createElement('script')
    s.src = `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(key)}&v=weekly&loading=async&libraries=marker&callback=__mapsReady`
    s.async = true
    s.onerror = () => {
      loading = undefined
      reject(new Error('Could not load Google Maps'))
    }
    document.head.append(s)
  })
  return loading
}

/** "14.5547, 121.0244" from a pin, as the pin field and the backend read it. */
export const pinText = (lat: number, lng: number) => `${lat.toFixed(6)}, ${lng.toFixed(6)}`

/** The pin in "lat, lng" text, or null. */
export function parsePin(text: string): { lat: number; lng: number } | null {
  const m = text.match(/(-?\d{1,2}\.\d+)\s*,\s*(-?\d{2,3}\.\d+)/)
  return m ? { lat: Number(m[1]), lng: Number(m[2]) } : null
}
