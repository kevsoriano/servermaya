import { onAuthStateChanged, signInAnonymously, signOut, type User } from 'firebase/auth'
import { useEffect, useState } from 'react'
import Boundary from './lib/Boundary'
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

/** Suri's logo, with a light wordmark in the dark theme. */
function Logo() {
  return (
    <picture>
      <source srcSet="/header-dark.png" media="(prefers-color-scheme: dark)" />
      <img src="/header-light.png" alt="Suri" width={302} height={96} />
    </picture>
  )
}

export default function App() {
  // undefined = still loading, null = signed out
  const [user, setUser] = useState<User | null>()
  const [me, setMe] = useState<Me>()
  const [error, setError] = useState('')
  const [tab, setTab] = useState<'conversations' | 'orders' | 'patterns' | 'settings'>('conversations')
  const [orderId, setOrderId] = useState<string>() // the order to open in the Orders tab, such as one opened from its chat
  const [chatId, setChatId] = useState<string>() // the chat to open in Conversations, such as one opened from a pattern

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

  const tryDemo = () => signInAnonymously(auth).catch((e) => setError(e.message))
  // A demo visitor who signs out cannot come back to the same demo, so say so first.
  const leave = () => (!me?.demo || confirm('Signing out ends this demo. Your demo chats and orders will not come back.')) && signOut(auth)

  if (user === undefined) return <main className="card">Loading…</main>

  if (!me)
    return (
      <main className="card">
        <h1 className="logo">
          <Logo />
        </h1>
        {error && <p role="alert">{error}</p>}
        {user ? (
          <button onClick={() => signOut(auth)}>Sign out</button>
        ) : (
          <>
            <p>
              Try it with demo chats: you are the seller, and AI plays each customer. Nothing reaches real customers, Messenger, or real
              money.
            </p>
            <p className="actions">
              <button className="primary" onClick={tryDemo}>
                Try the demo
              </button>
            </p>
          </>
        )}
      </main>
    )

  return (
    <div className="app">
      <header className="topbar">
        <span className="brand logo">
          <Logo />
        </span>
        <nav className="tabs">
          <button onClick={() => (setChatId(undefined), setTab('conversations'))} aria-current={tab === 'conversations'}>
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
      <Boundary key={tab}>
        {tab === 'conversations' ? (
          <Conversations
            key={chatId}
            initial={chatId}
            replyHours={me.reply_hours ?? 24}
            onOpenOrder={(id) => (setOrderId(id), setTab('orders'))}
          />
        ) : tab === 'orders' ? (
          <Orders key={orderId} initial={orderId} />
        ) : tab === 'patterns' ? (
          <Patterns onOpenChat={(id) => (setChatId(id), setTab('conversations'))} />
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
      </Boundary>
    </div>
  )
}
