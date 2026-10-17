import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import Confirm, { Paid } from './customer/Confirm.tsx'

// Customer pages are /c/<token>; /paid is where PayMongo sends a customer who paid from a link in the chat. Everything else
// is the seller app.
const token = location.pathname.match(/^\/c\/([\w-]+)$/)?.[1]

createRoot(document.getElementById('root')!).render(
  <StrictMode>{token ? <Confirm token={token} /> : location.pathname === '/paid' ? <Paid /> : <App />}</StrictMode>,
)
