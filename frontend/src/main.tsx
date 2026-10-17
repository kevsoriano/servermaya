import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { Paid } from './customer/Paid.tsx'

// /paid is where PayMongo sends a customer who paid from a link in the chat. Everything else is the seller app.
createRoot(document.getElementById('root')!).render(<StrictMode>{location.pathname === '/paid' ? <Paid /> : <App />}</StrictMode>)
