import { GoogleAuthProvider, onAuthStateChanged, signInWithPopup, signOut, type User } from 'firebase/auth'
import { useEffect, useState } from 'react'
import Conversations from './Conversations'
import Orders from './Orders'
import DispatchSettings from './DispatchSettings'
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

  if (user === undefined) return <main className="card">Loading…</main>

  if (!me)
    return (
      <main className="card">
        <h1>Seller Evidence Assistant</h1>
        {error && <p role="alert">{error}</p>}
        {user ? <button onClick={() => signOut(auth)}>Sign out</button> : <button className="primary" onClick={signIn}>Sign in with Google</button>}
      </main>
    )

  return (
    <div className="app">
      <header className="topbar">
        <strong className="brand">Seller Evidence Assistant</strong>
        <nav className="tabs">
          <button onClick={() => setTab('conversations')} aria-current={tab === 'conversations'}>
            Conversations
          </button>
          <button onClick={() => setTab('orders')} aria-current={tab === 'orders'}>
            Orders
          </button>
          <button onClick={() => setTab('settings')} aria-current={tab === 'settings'}>
            Settings
          </button>
        </nav>
        <span className="me" title={`${me.email}, verified by the backend`}>
          {me.name ?? me.email}
        </span>
        <button onClick={() => signOut(auth)}>Sign out</button>
      </header>
      {tab === 'conversations' ? (
        <Conversations />
      ) : tab === 'orders' ? (
        <Orders />
      ) : (
        <div className="page">
          <GcashSettings />
          <DispatchSettings />
        </div>
      )}
    </div>
  )
}
