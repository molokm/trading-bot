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
import { api } from './services/api'
import { ThemeProvider, useTheme } from './context/ThemeContext'
import { OnboardingProvider } from './context/OnboardingContext'
import { TranslationProvider, useTranslation } from './hooks/useTranslation'
import { GlossaryModal, OnboardingTour } from './components/ui'

const AuthContext = createContext()
export const useAuth = () => useContext(AuthContext)

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
  const [glossaryOpen, setGlossaryOpen] = useState(false)

  const isGuest = auth?.role === 'guest'
  const isAdmin = auth?.role === 'admin'

  useEffect(() => {
    const check = async () => {
      try {
        const h = await api.health()
        setHealth(h)
        setConnected(h.connected)
        setDemoMode(h.demo)
      } catch {
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
      <header className="flex items-center justify-between px-4 h-[var(--header-h)] border-b border-[var(--border)] bg-[var(--surface)] flex-shrink-0">
        {/* Left: Logo + Nav */}
        <div className="flex items-center gap-6">
          <div className="flex items-center gap-2">
            <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-[var(--profit)] to-[var(--info)] flex items-center justify-center">
              <TrendingUp size={14} className="text-white" />
            </div>
            <span className="text-sm font-bold text-[var(--txt)] tracking-tight hidden lg:inline">OKX Terminal</span>
          </div>

          <nav className="flex items-center gap-1">
            {navItems.map(item => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === '/'}
                className={({ isActive }) =>
                  `flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-all ${
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
        <div className="flex items-center gap-3">
          {/* Connection Status */}
          <div data-tour="status" className="flex items-center gap-2 px-2.5 py-1 rounded-md bg-[var(--bg)] border border-[var(--border)]">
            <span className={`w-2 h-2 rounded-full ${connected ? 'bg-[var(--profit)] animate-pulse-dot' : 'bg-[var(--loss)]'}`} />
            <span className={`text-2xs font-semibold ${connected ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
              {connected ? (demoMode ? 'DEMO' : 'LIVE') : 'OFFLINE'}
            </span>
          </div>

          {/* User role */}
          <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-[var(--bg)] border border-[var(--border)]">
            {isGuest ? <User size={12} className="text-[var(--info)]" /> : <Shield size={12} className="text-[var(--profit)]" />}
            <span className="text-2xs font-medium text-[var(--txt-secondary)]">{isGuest ? t('nav.guest') : t('nav.admin')}</span>
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
              <Route path="/mini" element={<MiniAppPage />} />
              <Route path="/*" element={<AppRouter />} />
            </Routes>
          </AuthContext.Provider>
        </OnboardingProvider>
      </TranslationProvider>
    </ThemeProvider>
  )
}
