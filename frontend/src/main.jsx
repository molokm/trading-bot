import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import './index.css'

// ── Global frontend error reporting (diagnose Telegram WebView white screens) ──
function reportClientError(payload) {
  try {
    fetch('/api/debug/client-error', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }).catch(() => {})
  } catch { /* ignore */ }
}
window.addEventListener('error', (e) => {
  reportClientError({
    type: 'error',
    message: String(e.message || ''),
    stack: String(e.error?.stack || '').slice(0, 3000),
    source: String(e.filename || ''),
    line: e.lineno,
    col: e.colno,
    href: window.location.href,
  })
})
window.addEventListener('unhandledrejection', (e) => {
  let reason = ''
  let stack = ''
  try { reason = String(e.reason && e.reason.message ? e.reason.message : e.reason) } catch { reason = String(e.reason) }
  try { stack = String(e.reason?.stack || '') } catch { stack = '' }
  reportClientError({ type: 'unhandledrejection', message: reason, stack: stack.slice(0, 3000), href: window.location.href })
})

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
)
