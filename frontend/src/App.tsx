import { GoogleAuthProvider, onAuthStateChanged, signInWithPopup, signOut, type User } from 'firebase/auth'
import { useEffect, useState } from 'react'
import Conversations from './Conversations'
import Orders from './Orders'
import GcashSettings from './GcashSettings'
import { api, auth } from './firebase'

type Me = { uid: string; email: string; name: string | null }

export default function App() {
  // undefined = still loading, null = signed out
  const [user, setUser] = useState<User | null>()
  const [me, setMe] = useState<Me>()
  const [error, setError] = useState('')
  const [tab, setTab] = useState<'conversations' | 'orders' | 'settings'>('conversations')

  useEffect(
    () =>
      onAuthStateChanged(auth, async (u) => {
        setUser(u)
        setMe(undefined)
        setError('')
        if (!u) return
        try {
          const r = await api('/me')
          if (r.ok) setMe(await r.json())
          else setError(r.status === 403 ? `${u.email} is not on the allowlist.` : `Backend returned ${r.status}.`)
        } catch {
          setError('Could not reach the backend.')
        }
      }),
    [],
  )

  const signIn = () =>
    signInWithPopup(auth, new GoogleAuthProvider()).catch((e) => setError(e.message))

  if (user === undefined) return <main>Loading…</main>

  return (
    <main>
      <h1>Seller Evidence Assistant</h1>
      {!user && <button onClick={signIn}>Sign in with Google</button>}
      {me && <p>Signed in as {me.name ?? me.email} ({me.email}), verified by the backend.</p>}
      {error && <p role="alert">{error}</p>}
      {me && (
        <nav>
          <button onClick={() => setTab('conversations')} aria-current={tab === 'conversations'}>
            Conversations
          </button>{' '}
          <button onClick={() => setTab('orders')} aria-current={tab === 'orders'}>
            Orders
          </button>{' '}
          <button onClick={() => setTab('settings')} aria-current={tab === 'settings'}>
            Settings
          </button>
        </nav>
      )}
      {me && (tab === 'conversations' ? <Conversations /> : tab === 'orders' ? <Orders /> : <GcashSettings />)}
      {user && <button onClick={() => signOut(auth)}>Sign out</button>}
    </main>
  )
}
