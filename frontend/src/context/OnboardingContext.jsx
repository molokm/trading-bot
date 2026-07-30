import { createContext, useContext, useState, useEffect } from 'react'
import { useTranslation } from '../hooks/useTranslation'

const OnboardingContext = createContext()

function getTourSteps(t) {
  return [
    {
      target: '[data-tour="status"]',
      title: t('onboarding.connection_status'),
      text: t('onboarding.connection_desc'),
    },
    {
      target: '[data-tour="metrics"]',
      title: t('onboarding.key_metrics'),
      text: t('onboarding.metrics_desc'),
    },
    {
      target: '[data-tour="chart"]',
      title: t('onboarding.chart_filters'),
      text: t('onboarding.chart_desc'),
    },
  ]
}

export function OnboardingProvider({ children }) {
  const { t } = useTranslation()
  const [active, setActive] = useState(false)
  const [step, setStep] = useState(0)

  const steps = getTourSteps(t)

  useEffect(() => {
    const seen = localStorage.getItem('onboarding_seen')
    if (!seen) {
      const timer = setTimeout(() => setActive(true), 800)
      return () => clearTimeout(timer)
    }
  }, [])

  const next = () => {
    if (step < steps.length - 1) {
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
    <OnboardingContext.Provider value={{ active, step, steps, next, prev, close, setActive }}>
      {children}
    </OnboardingContext.Provider>
  )
}

export const useOnboarding = () => useContext(OnboardingContext)
