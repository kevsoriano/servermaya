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

async function json<T>(r: Response): Promise<T> {
  if (!r.ok) {
    const detail = (await r.json().catch(() => null))?.detail
    // A request FastAPI could not validate lists each problem; anything else is one message.
    throw new Error(Array.isArray(detail) ? detail.map((d) => d.msg).join('; ') : (detail ?? `Backend returned ${r.status}`))
  }
  return r.json()
}

/** JSON from an authenticated backend route; throws with the backend's message on failure. */
export const load = <T>(path: string, init?: RequestInit) => api(path, init).then((r) => json<T>(r))

/** Demo chats: the customer, played by AI, answers the seller's latest message. */
export const customerReply = (conversationId: string) => load(`/conversations/${conversationId}/customer/auto`, { method: 'POST' })

/** JSON from a customer-link route, which needs no sign-in. */

// Firebase Hosting cuts off a request it forwards to Cloud Run after 60 s. Actions that call Gemini can take longer when it is
// slow, so a deployed build sends them straight to Cloud Run (VITE_API_URL, set by `make deploy-frontend`), which allows 300 s.
// Everything else stays same-origin. In dev, Vite's proxy has no such limit.
const DIRECT = import.meta.env.VITE_API_URL as string | undefined
const SLOW =
  /\/(claims\/[^/]+\/review|packing\/check|returns\/[^/]+\/check|draft-order|signals|brief|customer\/auto|gcash\/(read|history|sample-history))$/
const base = (path: string) => (DIRECT && SLOW.test(path.split('?')[0]) ? DIRECT : '') + '/api'

/** fetch() against the backend with the signed-in user's ID token attached. */
export async function api(path: string, init: RequestInit = {}) {
  const token = await auth.currentUser?.getIdToken()
  return fetch(`${base(path)}${path}`, {
    ...init,
    headers: { ...init.headers, Authorization: `Bearer ${token}` },
  }).catch(() => {
    throw new Error('Could not reach the server. Check your connection and try again.')
  })
}
