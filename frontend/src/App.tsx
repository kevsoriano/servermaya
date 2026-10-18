import { onAuthStateChanged, signInAnonymously, signOut, type User } from 'firebase/auth'
import { useEffect, useRef, useState } from 'react'
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
  /** this session has entered the access code: the only way in */
  admitted: boolean
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

  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const loads = useRef(0) // only the newest answer counts: an older one must not undo an access code just entered

  const loadMe = async () => {
    const n = ++loads.current
    try {
      const r = await api('/me', { signal: AbortSignal.timeout(20_000) })
      if (n !== loads.current) return
      if (r.ok) {
        const m: Me = await r.json()
        setMapsConfig(m.maps) // Google Maps for delivery pins, when the backend has a key
        setMe(m)
      } else setError(`Backend returned ${r.status}.`)
    } catch {
      if (n === loads.current) setError('Could not reach the backend.')
    }
  }

  useEffect(
    () =>
      onAuthStateChanged(auth, (u) => {
        setUser(u)
        setMe(undefined)
        setError('')
        if (u) loadMe()
      }),
    [], // eslint-disable-line react-hooks/exhaustive-deps -- subscribe once
  )

  // The one way in: an anonymous session, then the access code, entered once.
  const enter = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      // a Google session left from before access codes is not let in, so it is swapped for an anonymous one
      if (auth.currentUser && !auth.currentUser.isAnonymous) await signOut(auth)
      if (!auth.currentUser) await signInAnonymously(auth)
      const r = await api('/access', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }) })
      if (r.ok) {
        setCode('')
        await loadMe()
      } else setError((await r.json().catch(() => null))?.detail ?? `Backend returned ${r.status}.`)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }
  // A session that signs out cannot come back to the same demo chats, so say so first.
  const leave = () =>
    confirm('Signing out ends this session: your demo chats and orders will not come back, and you will need the access code again.') &&
    signOut(auth)

  if (user === undefined || (user && !me && !error)) return <main className="card">Loading…</main>

  if (!me?.admitted)
    return (
      <main className="card">
        <h1 className="logo">
          <Logo />
        </h1>
        <p>You are the seller: demo chats where AI plays each customer, and the shop's Messenger inbox. Nothing reaches real money.</p>
        <p>To get in, enter the access code from the "Try it" section of the GitHub repository's README.</p>
        {error && <p role="alert">{error}</p>}
        <form className="actions" onSubmit={enter}>
          <label>
            Access code <input value={code} onChange={(e) => setCode(e.target.value)} required autoComplete="off" spellCheck={false} />
          </label>
          <button className="primary" disabled={busy || !code.trim()}>
            {busy ? 'Checking…' : 'Enter'}
          </button>
        </form>
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
          Your own demo chats and orders, and the shop's Messenger inbox, shared by everyone with the code. Payments are PayMongo test mode
          and deliveries are the Lalamove sandbox.
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
