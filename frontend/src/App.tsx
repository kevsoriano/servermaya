import { GoogleAuthProvider, onAuthStateChanged, signInAnonymously, signInWithPopup, signOut, type User } from 'firebase/auth'
import { useEffect, useState } from 'react'
import { setMapsConfig } from './lib/maps'
import Conversations from './conversations/Conversations'
import Orders from './orders/Orders'
import Patterns from './patterns/Patterns'
import Catalog from './settings/Catalog'
import DispatchSettings from './settings/DispatchSettings'
import GcashSettings from './settings/GcashSettings'
import { api, auth } from './lib/firebase'

/** reply_hours: how long after the customer's last message Messenger lets a seller reply (24, or 168 with the Human Agent tag). */
type Me = {
  uid: string
  email: string
  name: string | null
  demo: boolean
  reply_hours?: number
  maps?: { key: string; map_id: string } | null
}

export default function App() {
  // undefined = still loading, null = signed out
  const [user, setUser] = useState<User | null>()
  const [me, setMe] = useState<Me>()
  const [error, setError] = useState('')
  const [tab, setTab] = useState<'conversations' | 'orders' | 'patterns' | 'settings'>('conversations')
  const [orderId, setOrderId] = useState<string>() // the order to open in the Orders tab, such as one opened from its chat

  useEffect(
    () =>
      onAuthStateChanged(auth, async (u) => {
        setUser(u)
        setMe(undefined)
        setError('')
        if (!u) return
        try {
          const r = await api('/me', { signal: AbortSignal.timeout(20_000) })
          if (r.ok) {
            const m: Me = await r.json()
            setMapsConfig(m.maps) // Google Maps for delivery pins, when the backend has a key
            setMe(m)
          } else setError(r.status === 403 ? `${u.email} is not on the allowlist.` : `Backend returned ${r.status}.`)
        } catch {
          setError('Could not reach the backend.')
        }
      }),
    [],
  )

  const signIn = () => signInWithPopup(auth, new GoogleAuthProvider()).catch((e) => setError(e.message))
  const tryDemo = () => signInAnonymously(auth).catch((e) => setError(e.message))
  // A demo visitor who signs out cannot come back to the same demo, so say so first.
  const leave = () => (!me?.demo || confirm('Signing out ends this demo. Your demo chats and orders will not come back.')) && signOut(auth)

  if (user === undefined) return <main className="card">Loading…</main>

  if (!me)
    return (
      <main className="card">
        <h1>Seller Evidence Assistant</h1>
        {error && <p role="alert">{error}</p>}
        {user ? (
          <button onClick={() => signOut(auth)}>Sign out</button>
        ) : (
          <>
            <p>
              Try it with demo chats: you play both the seller and the customer. Nothing reaches real customers, Messenger, or real money.
            </p>
            <p className="actions">
              <button className="primary" onClick={tryDemo}>
                Try the demo
              </button>
              <button onClick={signIn}>Seller sign-in with Google</button>
            </p>
          </>
        )}
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
          <button onClick={() => (setOrderId(undefined), setTab('orders'))} aria-current={tab === 'orders'}>
            Orders
          </button>
          <button onClick={() => setTab('patterns')} aria-current={tab === 'patterns'}>
            Patterns
          </button>
          <button onClick={() => setTab('settings')} aria-current={tab === 'settings'}>
            Settings
          </button>
        </nav>
        <span className="me" title={`${me.email}, verified by the backend`}>
          {me.name ?? me.email}
        </span>
        <button onClick={leave}>Sign out</button>
      </header>
      {me.demo && (
        <p className="demo-banner" role="note">
          You see only your own demo chats and orders. Payments are PayMongo test mode and deliveries are the Lalamove sandbox.
        </p>
      )}
      {tab === 'conversations' ? (
        <Conversations replyHours={me.reply_hours ?? 24} onOpenOrder={(id) => (setOrderId(id), setTab('orders'))} />
      ) : tab === 'orders' ? (
        <Orders key={orderId} initial={orderId} />
      ) : tab === 'patterns' ? (
        <Patterns />
      ) : (
        <div className="page">
          {me.demo && (
            <p className="demo-banner" role="note">
              A made-up catalog, GCash account and pickup address. They can't be changed in the demo.
            </p>
          )}
          <Catalog readOnly={!!me.demo} />
          <GcashSettings />
          <DispatchSettings readOnly={!!me.demo} />
        </div>
      )}
    </div>
  )
}
