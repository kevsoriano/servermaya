import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import Confirm from './customer/Confirm.tsx'

// Customer confirmation links are /c/<token>. Everything else is the seller app.
const token = location.pathname.match(/^\/c\/([\w-]+)$/)?.[1]

createRoot(document.getElementById('root')!).render(<StrictMode>{token ? <Confirm token={token} /> : <App />}</StrictMode>)
