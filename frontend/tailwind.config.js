/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // Accent palette
        profit: {
          DEFAULT: '#00ff88',
          dim: 'rgba(0, 255, 136, 0.15)',
          border: 'rgba(0, 255, 136, 0.3)',
        },
        loss: {
          DEFAULT: '#ff3366',
          dim: 'rgba(255, 51, 102, 0.15)',
          border: 'rgba(255, 51, 102, 0.3)',
        },
        accent: {
          blue: '#4a9eff',
          orange: '#ff9500',
          purple: '#7c3aed',
          yellow: '#ffd700',
        },
        // Surface
        surface: {
          DEFAULT: 'var(--surface)',
          raised: 'var(--surface-raised)',
          overlay: 'var(--surface-overlay)',
          border: 'var(--border)',
          'border-hover': 'var(--border-hover)',
        },
        // Text
        txt: {
          DEFAULT: 'var(--txt)',
          secondary: 'var(--txt-secondary)',
          muted: 'var(--txt-muted)',
        },
        // Background
        bg: {
          DEFAULT: 'var(--bg)',
          alt: 'var(--bg-alt)',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'Fira Code', 'monospace'],
      },
      fontSize: {
        '2xs': ['0.65rem', { lineHeight: '0.9rem' }],
      },
      spacing: {
        '4.5': '1.125rem',
        '13': '3.25rem',
        '18': '4.5rem',
        '88': '22rem',
        '120': '30rem',
      },
      borderRadius: {
        '4xl': '2rem',
      },
      boxShadow: {
        'glow-green': '0 0 20px rgba(0, 255, 136, 0.15)',
        'glow-red': '0 0 20px rgba(255, 51, 102, 0.15)',
        'glow-blue': '0 0 20px rgba(74, 158, 255, 0.15)',
      },
      animation: {
        'pulse-dot': 'pulse-dot 2s ease-in-out infinite',
        'slide-in': 'slide-in 0.3s ease-out',
        'slide-up': 'slide-up 0.3s ease-out',
        'fade-in': 'fade-in 0.2s ease-out',
        'shimmer': 'shimmer 2s linear infinite',
      },
      keyframes: {
        'pulse-dot': {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.4' },
        },
        'slide-in': {
          '0%': { transform: 'translateX(100%)' },
          '100%': { transform: 'translateX(0)' },
        },
        'slide-up': {
          '0%': { transform: 'translateY(10px)', opacity: '0' },
          '100%': { transform: 'translateY(0)', opacity: '1' },
        },
        'fade-in': {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        'shimmer': {
          '0%': { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
      },
    },
  },
  plugins: [],
}
