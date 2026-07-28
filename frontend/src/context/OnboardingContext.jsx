import { createContext, useContext, useState, useEffect } from 'react'

const OnboardingContext = createContext()

const TOUR_STEPS = [
  {
    target: '[data-tour="status"]',
    title: 'Статус подключения',
    text: 'Здесь отображается текущий режим — LIVE (реальные средства) или DEMO (тестовые). Зелёный индикатор означает активное подключение к OKX.',
  },
  {
    target: '[data-tour="metrics"]',
    title: 'Ключевые метрики',
    text: '«Золотая зона» — основные показатели вашего портфеля. Баланс, открытые позиции и PnL обновляются в реальном времени.',
  },
  {
    target: '[data-tour="chart"]',
    title: 'График и фильтры',
    text: 'Свечной график с наложением сделок бота. Используйте фильтры-чипсы для быстрого поиска по инструменту, времени и результату.',
  },
]

export function OnboardingProvider({ children }) {
  const [active, setActive] = useState(false)
  const [step, setStep] = useState(0)

  useEffect(() => {
    const seen = localStorage.getItem('onboarding_seen')
    if (!seen) {
      const timer = setTimeout(() => setActive(true), 800)
      return () => clearTimeout(timer)
    }
  }, [])

  const next = () => {
    if (step < TOUR_STEPS.length - 1) {
      setStep(s => s + 1)
    } else {
      close()
    }
  }

  const prev = () => setStep(s => Math.max(0, s - 1))

  const close = () => {
    setActive(false)
    setStep(0)
    localStorage.setItem('onboarding_seen', '1')
  }

  return (
    <OnboardingContext.Provider value={{ active, step, steps: TOUR_STEPS, next, prev, close, setActive }}>
      {children}
    </OnboardingContext.Provider>
  )
}

export const useOnboarding = () => useContext(OnboardingContext)