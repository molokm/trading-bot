import React, { createContext, useContext, useCallback } from 'react'
import ru from '../i18n/ru'

const translations = { ru }
const TranslationContext = createContext()

export function TranslationProvider({ children }) {
  const t = useCallback((key, vars = {}) => {
    let text = ru[key] || key
    Object.entries(vars).forEach(([k, v]) => {
      text = text.replace(`{${k}}`, v)
    })
    return text
  }, [])
  return (
    <TranslationContext.Provider value={{ t, lang: 'ru' }}>
      {children}
    </TranslationContext.Provider>
  )
}

export function useTranslation() {
  return useContext(TranslationContext)
}
