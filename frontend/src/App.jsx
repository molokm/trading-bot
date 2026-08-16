import React, { useState, useEffect, createContext, useContext, lazy, Suspense } from 'react'
import { Routes, Route, NavLink } from 'react-router-dom'
import {
  LayoutDashboard, Bot, BarChart3, ScrollText, Settings,
  TrendingUp, LogOut, User, Shield, Sun, Moon, HelpCircle, Globe
} from 'lucide-react'
import LoginPage from './pages/LoginPage'
import { Loader } from './components/ui'

const Dashboard = lazy(() => import('./pages/Dashboard'))
const BotsPage = lazy(() => import('./pages/BotsPage'))
const BacktestPage = lazy(() => import('./pages/BacktestPage'))
const ChartPage = lazy(() => import('./pages/ChartPage'))
const HistoryPage = lazy(() => import('./pages/HistoryPage'))
const SettingsPage = lazy(() => import('./pages/SettingsPage'))
const DocsPage = lazy(() => import('./pages/DocsPage'))
const MiniAppPage = lazy(() => import('./pages/MiniAppPage'))
const TrackerPage = lazy(() => import('./pages/TrackerPage'))
import { api } from './services/api'
import { ThemeProvider, useTheme } from './context/ThemeContext'
import { OnboardingProvider } from './context/OnboardingContext'
import { TranslationProvider, useTranslation } from './hooks/useTranslation'
import { GlossaryModal, OnboardingTour } from './components/ui'

const AuthContext = createContext()
export const useAuth = () => useContext(AuthContext)

/* ═══ ErrorBoundary — показать ошибку вместо белого экрана ═══ */
class MiniErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { error: null, stack: '', compStack: '' }
  }
  static getDerivedStateFromError(error) {
    return {
      error: String(error && error.message ? error.message : error),
      stack: String(error && error.stack ? error.stack : '').slice(0, 1200),
    }
  }
  componentDidCatch(error, info) {
    this.setState({ compStack: String(info?.componentStack || '').slice(0, 800) })
    try {
      fetch('/api/debug/client-error', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          type: 'react',
          message: String(error && error.message ? error.message : error),
          stack: String(error?.stack || '').slice(0, 3000),
          component: String(info?.componentStack || '').slice(0, 1500),
        }),
      }).catch(() => {})
    } catch { /* ignore */ }
  }
  render() {
    if (this.state.error) {
      return (
        <div className="h-screen flex flex-col items-center justify-center gap-3 p-6 bg-[var(--bg)] text-center">
          <div className="text-3xl">⚠️</div>
          <div className="text-sm font-semibold text-[var(--loss)]">Произошла ошибка</div>
          <pre className="text-2xs text-[var(--txt-secondary)] whitespace-pre-wrap break-words max-w-full max-h-64 overflow-y-auto">{this.state.error}</pre>
          {this.state.stack && (
            <pre className="text-2xs text-[var(--txt-muted)] whitespace-pre-wrap break-words max-w-full max-h-64 overflow-y-auto text-left border border-[var(--border)] rounded p-2">{this.state.stack}</pre>
          )}
          <button className="btn btn-primary" onClick={() => window.location.reload()}>Перезагрузить</button>
        </div>
      )
    }
    return this.props.children
  }
}

function AppRouter() {
  const { auth, setAuth } = useAuth()
  if (!auth) return <LoginPage onLogin={(token, role) => setAuth({ token, role })} />
  return <AppLayout />
}

function AppLayout() {
  const { auth, setAuth } = useAuth()
  const { theme, toggle } = useTheme()
  const { t, lang, setLang } = useTranslation()
  const [connected, setConnected] = useState(false)
  const [demoMode, setDemoMode] = useState(true)
  const [health, setHealth] = useState({ status: 'checking' })
  const [latencyMs, setLatencyMs] = useState(null)
  const [glossaryOpen, setGlossaryOpen] = useState(false)

  const isGuest = auth?.role === 'guest'
  const isAdmin = auth?.role === 'admin'

  useEffect(() => {
    const check = async () => {
      const t0 = performance.now()
      try {
        const h = await api.health()
        setLatencyMs(Math.round(performance.now() - t0))
        setHealth(h)
        setConnected(h.connected)
        setDemoMode(h.demo)
      } catch {
        setLatencyMs(null)
        setHealth({ status: 'error' })
        setConnected(false)
      }
    }
    check()
    const interval = setInterval(check, 15000)
    return () => clearInterval(interval)
  }, [])

  const handleLogout = () => {
    api.logout().catch(() => {})
    localStorage.removeItem('auth_token')
    localStorage.removeItem('auth_role')
    setAuth(null)
  }

  const navItems = [
    { to: '/', icon: LayoutDashboard, label: t('nav.dashboard') },
    { to: '/bots', icon: Bot, label: t('nav.bots') },
    { to: '/backtest', icon: BarChart3, label: t('nav.backtest') },
    { to: '/chart', icon: BarChart3, label: t('nav.chart') },
    { to: '/history', icon: ScrollText, label: t('nav.history') },
    ...(isAdmin ? [{ to: '/settings', icon: Settings, label: t('nav.settings') }] : []),
  ]

  return (
    <div className="h-screen flex flex-col bg-[var(--bg)] overflow-hidden">
      {/* ═══ HEADER ═══ */}
      <header className="flex items-center justify-between px-5 h-[var(--header-h)] border-b border-[var(--border)] bg-[var(--surface)] flex-shrink-0">
        {/* Left: Logo + Nav */}
        <div className="flex items-center gap-7">
          <div className="flex items-center gap-2">
            <span className="text-[17px] font-bold text-[var(--txt)] tracking-tight hidden lg:inline">COPIX</span>
          </div>

          <nav className="flex items-center gap-1">
            {navItems.map(item => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === '/'}
                className={({ isActive }) =>
                  `flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-all ${
                    isActive
                      ? 'bg-[var(--info-dim)] text-[var(--info)]'
                      : 'text-[var(--txt-muted)] hover:text-[var(--txt)] hover:bg-[var(--surface-overlay)]'
                  }`
                }
              >
                <item.icon size={14} />
                <span className="hidden md:inline">{item.label}</span>
              </NavLink>
            ))}
          </nav>
        </div>

        {/* Right: Status + Controls */}
        <div className="flex items-center gap-2">
          {/* Connection Status */}
          <div data-tour="status" className="flex items-center gap-2 px-2.5 py-1 rounded-lg bg-[var(--bg)] border border-[var(--border)]">
            <span className={`w-1.5 h-1.5 rounded-full ${connected ? 'bg-[var(--profit)] animate-pulse-dot' : 'bg-[var(--loss)]'}`} />
            <span className={`text-2xs font-semibold ${connected ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
              {connected ? (demoMode ? 'DEMO' : 'LIVE') : 'OFFLINE'}
            </span>
            {health?.version ? (
              <span className="text-[10px] font-mono text-[var(--txt-muted)] hidden md:inline" title="Build / git commit">
                {String(health.version).slice(0, 7)}
              </span>
            ) : null}
            {latencyMs != null ? (
              <span className="text-[10px] font-mono text-[var(--txt-muted)] hidden md:inline" title={t('nav.latency_tip')}>
                {latencyMs}ms
              </span>
            ) : null}
          </div>

          {/* User role */}
          <div
            className={`flex items-center gap-1.5 px-2.5 py-1 rounded-lg border ${
              isGuest
                ? 'bg-amber-500/10 border-amber-500/30'
                : 'bg-[var(--bg)] border-[var(--border)]'
            }`}
            title={isGuest ? t('nav.guest_tip') : t('nav.admin')}
          >
            {isGuest ? <User size={12} className="text-amber-400" /> : <Shield size={12} className="text-[var(--profit)]" />}
            <span className={`text-2xs font-medium ${isGuest ? 'text-amber-300' : 'text-[var(--txt-secondary)]'}`}>
              {isGuest ? t('nav.guest_readonly') : t('nav.admin')}
            </span>
          </div>

          {/* Glossary */}
          <button className="btn-icon" onClick={() => setGlossaryOpen(true)} title={t('nav.glossary')}>
            <HelpCircle size={15} />
          </button>

          {/* Language toggle */}
          <button className="btn-icon" onClick={() => setLang(lang === 'ru' ? 'en' : 'ru')} title={lang === 'ru' ? 'English' : 'Русский'}>
            <Globe size={15} />
            <span className="text-[10px] font-bold ml-0.5">{lang.toUpperCase()}</span>
          </button>

          {/* Theme toggle */}
          <button className="btn-icon" onClick={toggle} title={theme === 'dark' ? t('nav.light_theme') : t('nav.dark_theme')}>
            {theme === 'dark' ? <Sun size={15} /> : <Moon size={15} />}
          </button>

          {/* Logout */}
          <button className="btn-icon" onClick={handleLogout} title={t('nav.logout')}>
            <LogOut size={15} />
          </button>
        </div>
      </header>

      {isGuest && (
        <div className="flex-shrink-0 px-4 py-1.5 text-center text-2xs bg-amber-500/10 border-b border-amber-500/20 text-amber-200/90">
          {t('nav.guest_banner')}
        </div>
      )}

      {/* ═══ MAIN CONTENT ═══ */}
      <main className="flex-1 overflow-hidden">
        <Suspense fallback={<div className="flex items-center justify-center h-full"><Loader /></div>}>
        <Routes>
          <Route path="/" element={<Dashboard health={health} connected={connected} isGuest={isGuest} demoMode={demoMode} />} />
          <Route path="/bots" element={<BotsPage connected={connected} isGuest={isGuest} />} />
          <Route path="/backtest" element={<BacktestPage connected={connected} />} />
          <Route path="/chart" element={<ChartPage />} />
          <Route path="/history" element={<HistoryPage />} />
          <Route path="/settings" element={<SettingsPage onConnected={setConnected} onDemoMode={setDemoMode} />} />
          <Route path="/docs" element={<DocsPage />} />
        </Routes>
        </Suspense>
      </main>

      {/* ═══ MODALS ═══ */}
      <GlossaryModal open={glossaryOpen} onClose={() => setGlossaryOpen(false)} />
      <OnboardingTour />
    </div>
  )
}

export default function App() {
  const [auth, setAuth] = useState(() => {
    const token = localStorage.getItem('auth_token')
    const role = localStorage.getItem('auth_role')
    return token ? { token, role } : null
  })

  return (
    <ThemeProvider>
      <TranslationProvider>
        <OnboardingProvider>
          <AuthContext.Provider value={{ auth, setAuth }}>
            <Routes>
              <Route path="/login" element={<LoginPage onLogin={(token, role) => setAuth({ token, role })} />} />
              <Route path="/mini" element={<MiniErrorBoundary><MiniAppPage /></MiniErrorBoundary>} />
              <Route path="/tracker" element={<TrackerPage />} />
              <Route path="/*" element={<AppRouter />} />
            </Routes>
          </AuthContext.Provider>
        </OnboardingProvider>
      </TranslationProvider>
    </ThemeProvider>
  )
}
