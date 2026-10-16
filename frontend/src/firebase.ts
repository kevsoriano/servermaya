import { initializeApp } from 'firebase/app'
import { getAuth } from 'firebase/auth'

// Public identifiers, not secrets.
const app = initializeApp({
  apiKey: 'AIzaSyCpPVizoaE1xP_l2dr8QjK68zTJStKX6fk',
  authDomain: 'servermaya-ai-builder-cup.firebaseapp.com',
  projectId: 'servermaya-ai-builder-cup',
  appId: '1:628763008062:web:a4072a64c9eb3f0ba0c56f',
})

export const auth = getAuth(app)

export const peso = (centavos: number | null) =>
  centavos === null ? 'not stated' : (centavos / 100).toLocaleString('en-PH', { style: 'currency', currency: 'PHP' })

/** 639171231234 -> 0917 123 1234 */
export const localMobile = (m: string) => `0${m.slice(2, 5)} ${m.slice(5, 8)} ${m.slice(8)}`

async function json<T>(r: Response): Promise<T> {
  if (!r.ok) throw new Error((await r.json().catch(() => null))?.detail ?? `Backend returned ${r.status}`)
  return r.json()
}

/** JSON from an authenticated backend route; throws with the backend's message on failure. */
export const load = <T,>(path: string, init?: RequestInit) => api(path, init).then((r) => json<T>(r))

/** JSON from a customer-link route, which needs no sign-in. */
export const loadPublic = <T,>(path: string, init?: RequestInit) => fetch(`/api/public${path}`, init).then((r) => json<T>(r))

/** fetch() against the backend with the signed-in user's ID token attached. */
export async function api(path: string, init: RequestInit = {}) {
  const token = await auth.currentUser?.getIdToken()
  return fetch(`/api${path}`, {
    ...init,
    headers: { ...init.headers, Authorization: `Bearer ${token}` },
  })
}

export const STATUS = {
  draft: 'Draft',
  awaiting_confirmation: 'Waiting for customer to confirm',
  confirmed: 'Confirmed',
  cancelled: 'Cancelled',
}

export const CLAIM_TYPES: Record<string, string> = {
  missing_item: 'Missing item',
  wrong_item: 'Wrong item',
  damage: 'Damaged',
  nonreceipt: 'Not received',
  payment_issue: 'Payment issue',
  cancellation: 'Cancellation',
  other: 'Other',
}

/** Orders drafted from a chat, real Messenger or demo, have messages to check and a thread to reply in. */
export const fromChat = (o: { source?: { type: string } }) => o.source?.type === 'messenger' || o.source?.type === 'demo'
