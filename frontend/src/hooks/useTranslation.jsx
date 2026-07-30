import React, { createContext, useContext, useCallback, useState, useEffect } from 'react'
import ru from '../i18n/ru'
import en from '../i18n/en'

const translations = { ru, en }
const TranslationContext = createContext()
const STORAGE_KEY = 'app_lang'
const DEFAULT_LANG = 'ru'

export function TranslationProvider({ children }) {
  const [lang, setLangState] = useState(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY)
      if (stored && translations[stored]) return stored
    } catch {}
    return DEFAULT_LANG
  })

  useEffect(() => {
    try { localStorage.setItem(STORAGE_KEY, lang) } catch {}
  }, [lang])

  const setLang = useCallback((newLang) => {
    if (translations[newLang]) setLangState(newLang)
  }, [])

  const t = useCallback((key, vars = {}) => {
    const dict = translations[lang] || translations[DEFAULT_LANG]
    let text = dict[key] || translations[DEFAULT_LANG][key] || key
    Object.entries(vars).forEach(([k, v]) => {
      text = text.replace(new RegExp(`\\{${k}\\}`, 'g'), String(v))
    })
    return text
  }, [lang])

  const locale = lang === 'ru' ? 'ru-RU' : 'en-US'

  return (
    <TranslationContext.Provider value={{ t, lang, setLang, locale }}>
      {children}
    </TranslationContext.Provider>
  )
}

export function useTranslation() {
  return useContext(TranslationContext)
}
