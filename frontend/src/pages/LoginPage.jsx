import React, { useState, useEffect } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Lock, Eye, EyeOff, Loader2, Shield, User } from 'lucide-react'
import { api } from '../services/api'
import { useTranslation } from '../hooks/useTranslation'

export default function LoginPage({ onLogin }) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const [password, setPassword] = useState('')
  const [showPw, setShowPw] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [hasPassword, setHasPassword] = useState(true)
  const [searchParams] = useSearchParams()

  useEffect(() => {
    if (searchParams.get('reason') === 'session') {
      setError(t('login.session_expired'))
    }
  }, [searchParams, t])

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
      navigate('/')
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
      navigate('/')
    } catch (e) {
      setError(e.message)
    }
    setLoading(false)
  }

  return (
    <div className="relative min-h-screen flex items-center justify-center p-4 overflow-hidden">
      {/* Animated background gradient */}
      <div className="absolute inset-0 login-bg-anim" />
      <div className="relative panel w-full max-w-sm" style={{ maxWidth: 380 }}>
        <div className="p-8 space-y-6">
          <div className="text-center space-y-3">
            <div>
              <h1 className="text-2xl font-bold text-[var(--txt)] tracking-tight">COPIX</h1>
              <p className="text-xs text-[var(--txt-muted)] mt-1">{t('login.title')}</p>
            </div>
          </div>

          {error && (
            <div className="bg-[var(--loss-dim)] border border-[var(--loss)]/20 rounded-lg p-3 text-xs text-[var(--loss)] text-center">
              {error}
            </div>
          )}

          {hasPassword && (
            <div className="space-y-2">
              <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider">{t('login.password')}</label>
              <div className="relative">
                <input
                  type={showPw ? 'text' : 'password'}
                  className="w-full pr-10"
                  placeholder={t('login.placeholder')}
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                  onKeyDown={e => e.key === 'Enter' && handleLogin()}
                  autoFocus
                />
                <button
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-[var(--txt-muted)] hover:text-[var(--txt)]"
                  onClick={() => setShowPw(!showPw)}
                  tabIndex={-1}
                >
                  {showPw ? <EyeOff size={16} /> : <Eye size={16} />}
                </button>
              </div>
              <button
                className="btn btn-primary w-full py-2.5"
                onClick={handleLogin}
                disabled={loading || !password}
              >
                {loading ? <Loader2 size={14} className="animate-spin" /> : <Lock size={14} />}
                {loading ? t('login.submitting') : t('login.submit')}
              </button>
            </div>
          )}

          <div className="relative">
            <div className="absolute inset-0 flex items-center"><div className="w-full border-t border-[var(--border)]" /></div>
            <div className="relative flex justify-center"><span className="bg-[var(--surface)] px-2 text-2xs text-[var(--txt-muted)]">{t('login.or')}</span></div>
          </div>

          <button
            className="btn btn-ghost w-full py-2.5"
            onClick={handleGuest}
            disabled={loading}
          >
            {loading ? <Loader2 size={14} className="animate-spin" /> : <User size={14} />}
            {t('login.guest')}
          </button>

          {!hasPassword && (
            <p className="text-2xs text-center text-[var(--txt-muted)]">{t('login.no_password')}</p>
          )}

          <div className="flex items-center justify-center gap-2 text-2xs text-[var(--txt-muted)] pt-2">
            <Shield size={12} />
            <span>{t('login.https_note')}</span>
          </div>
        </div>
      </div>
    </div>
  )
}
