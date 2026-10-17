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
  if (!r.ok) throw new Error((await r.json().catch(() => null))?.detail ?? `Backend returned ${r.status}`)
  return r.json()
}

/** JSON from an authenticated backend route; throws with the backend's message on failure. */
export const load = <T>(path: string, init?: RequestInit) => api(path, init).then((r) => json<T>(r))

/** Demo chats: the customer, played by AI, answers the seller's latest message. */
export const customerReply = (conversationId: string) => load(`/conversations/${conversationId}/customer/auto`, { method: 'POST' })

/** JSON from a customer-link route, which needs no sign-in. */

/** fetch() against the backend with the signed-in user's ID token attached. */
export async function api(path: string, init: RequestInit = {}) {
  const token = await auth.currentUser?.getIdToken()
  return fetch(`/api${path}`, {
    ...init,
    headers: { ...init.headers, Authorization: `Bearer ${token}` },
  })
}
