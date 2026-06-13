import React, { useState, useEffect } from 'react'
import { Routes, Route, NavLink } from 'react-router-dom'
import {
  LayoutDashboard, Settings, BarChart3, ScrollText,
  Wallet, Activity, TrendingUp, Play, Circle, Bot, CandlestickChart
} from 'lucide-react'
import Dashboard from './pages/Dashboard'
import SettingsPage from './pages/SettingsPage'
import StrategiesPage from './pages/StrategiesPage'
import TradeLogPage from './pages/TradeLogPage'
import LiveTrading from './pages/LiveTrading'
import ChartPage from './pages/ChartPage'
import { api } from './services/api'
import { TranslationProvider, useTranslation } from './hooks/useTranslation'

function AppContent() {
  const { t } = useTranslation()
  const [connected, setConnected] = useState(false)
  const [demoMode, setDemoMode] = useState(true)
  const [health, setHealth] = useState({ status: 'checking' })

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

  const navItems = [
    { to: '/', icon: LayoutDashboard, label: t('nav.overview') },
    { to: '/chart', icon: CandlestickChart, label: 'График' },
    { to: '/settings', icon: Settings, label: t('nav.settings') },
    { to: '/strategies', icon: BarChart3, label: t('nav.strategies') },
    { to: '/trades', icon: ScrollText, label: t('nav.trade_log') },
    { to: '/live', icon: Bot, label: 'Лайв боты' },
  ]

  return (
    <div className="min-h-screen bg-dark-bg flex">
      {/* Sidebar */}
      <aside className="w-64 glass m-4 rounded-2xl flex flex-col border-r-0" style={{border: '1px solid rgba(255,255,255,0.06)'}}>
        <div className="p-5 border-b border-white/5">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-lg bg-gradient-to-br from-neon-green to-neon-blue flex items-center justify-center">
              <TrendingUp size={18} className="text-white" />
            </div>
            <div>
              <h1 className="text-lg font-bold text-white tracking-tight">OKX Terminal</h1>
              <div className="flex items-center gap-2 mt-0.5">
                <span className={`status-dot ${connected ? 'online' : 'offline'}`} />
                <span className="text-xs text-gray-400">
                  {connected ? t('nav.connected') : t('nav.offline')}
                </span>
              </div>
            </div>
          </div>
        </div>

        <nav className="flex-1 p-3 space-y-1">
          {navItems.map(item => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === '/'}
              className={({ isActive }) =>
                `flex items-center gap-3 px-4 py-3 rounded-xl text-sm font-medium transition-all ${
                  isActive
                    ? 'bg-white/5 text-neon-green neon-glow-green'
                    : 'text-gray-400 hover:text-white hover:bg-white/5'
                }`
              }
            >
              <item.icon size={18} />
              {item.label}
            </NavLink>
          ))}
        </nav>

        <div className="p-4 border-t border-white/5">
          <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-white/5">
            <Wallet size={14} className="text-gray-400" />
            <span className={`text-xs font-medium ${demoMode ? 'text-neon-yellow' : 'text-neon-green'}`}>
              {demoMode ? t('sidebar.demo') : t('sidebar.live')}
            </span>
          </div>
        </div>
      </aside>

      {/* Main */}
      <main className="flex-1 p-6 overflow-auto">
        <Routes>
          <Route path="/" element={<Dashboard health={health} connected={connected} />} />
          <Route path="/settings" element={<SettingsPage onConnected={setConnected} onDemoMode={setDemoMode} />} />
          <Route path="/strategies" element={<StrategiesPage />} />
          <Route path="/trades" element={<TradeLogPage />} />
          <Route path="/live" element={<LiveTrading />} />
          <Route path="/chart" element={<ChartPage />} />
        </Routes>
      </main>
    </div>
  )
}

export default function App() {
  return (
    <TranslationProvider>
      <AppContent />
    </TranslationProvider>
  )
}
