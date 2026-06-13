import React, { useState, useEffect } from 'react'
import { Lock, Eye, EyeOff, Loader2, TrendingUp, Shield, User } from 'lucide-react'
import { api } from '../services/api'

export default function LoginPage({ onLogin }) {
  const [password, setPassword] = useState('')
  const [showPw, setShowPw] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [hasPassword, setHasPassword] = useState(true)

  useEffect(() => {
    api.authStatus().then(s => setHasPassword(s.has_password)).catch(() => {})
  }, [])

  const handleLogin = async () => {
    setLoading(true)
    setError('')
    try {
      const res = await api.login(password)
      localStorage.setItem('auth_token', res.token)
      localStorage.setItem('auth_role', res.role)
      onLogin?.(res.token, res.role)
    } catch (e) {
      setError(e.message)
    }
    setLoading(false)
  }

  const handleGuest = async () => {
    setLoading(true)
    try {
      const res = await api.guest()
      localStorage.setItem('auth_token', res.token)
      localStorage.setItem('auth_role', res.role)
      onLogin?.(res.token, res.role)
    } catch (e) {
      setError(e.message)
    }
    setLoading(false)
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter') handleLogin()
  }

  return (
    <div className="min-h-screen bg-dark-bg flex items-center justify-center p-4">
      <div className="glass rounded-2xl p-8 w-full max-w-sm space-y-6" style={{border: '1px solid rgba(255,255,255,0.06)'}}>
        <div className="text-center space-y-3">
          <div className="w-14 h-14 rounded-xl bg-gradient-to-br from-neon-green to-neon-blue flex items-center justify-center mx-auto">
            <TrendingUp size={28} className="text-white" />
          </div>
          <h1 className="text-xl font-bold text-white">OKX Terminal</h1>
          <p className="text-sm text-gray-400">Вход в панель управления</p>
        </div>

        {error && (
          <div className="bg-neon-red/5 border border-neon-red/20 rounded-xl p-3 text-sm text-neon-red text-center">
            {error}
          </div>
        )}

        {hasPassword && (
          <div className="space-y-2">
            <label className="text-xs font-medium text-gray-400 uppercase tracking-wider">Пароль</label>
            <div className="relative">
              <input
                type={showPw ? 'text' : 'password'}
                className="w-full pr-10"
                placeholder="Введите пароль"
                value={password}
                onChange={e => setPassword(e.target.value)}
                onKeyDown={handleKeyDown}
                autoFocus
              />
              <button
                className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-white"
                onClick={() => setShowPw(!showPw)}
                tabIndex={-1}
              >
                {showPw ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>
            <button
              className="btn-neon w-full py-2.5 rounded-xl text-sm font-semibold flex items-center justify-center gap-2 mt-3"
              onClick={handleLogin}
              disabled={loading || !password}
            >
              {loading ? <Loader2 size={14} className="animate-spin" /> : <Lock size={14} />}
              {loading ? 'Вход...' : 'Войти'}
            </button>
          </div>
        )}

        <div className="relative">
          <div className="absolute inset-0 flex items-center">
            <div className="w-full border-t border-white/5" />
          </div>
          <div className="relative flex justify-center text-xs">
            <span className="bg-dark-bg px-2 text-gray-500">или</span>
          </div>
        </div>

        <button
          className="w-full py-2.5 rounded-xl text-sm font-semibold flex items-center justify-center gap-2 border border-white/10 text-gray-300 hover:bg-white/5 hover:text-white transition-all"
          onClick={handleGuest}
          disabled={loading}
        >
          {loading ? <Loader2 size={14} className="animate-spin" /> : <User size={14} />}
          Гостевой режим (только просмотр)
        </button>

        {!hasPassword && (
          <p className="text-xs text-center text-gray-500">
            Пароль не установлен — вход без авторизации
          </p>
        )}

        <div className="flex items-center justify-center gap-2 text-xs text-gray-500 pt-2">
          <Shield size={12} />
          <span>Все данные передаются по HTTPS</span>
        </div>
      </div>
    </div>
  )
}
