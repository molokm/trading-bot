/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        neon: {
          green: '#00ff88',
          red: '#ff3366',
          blue: '#00d4ff',
          purple: '#7c3aed',
          yellow: '#ffd700',
        },
        dark: {
          bg: '#0a0a1a',
          card: 'rgba(15, 15, 35, 0.8)',
          border: 'rgba(255, 255, 255, 0.08)',
        }
      },
      backdropBlur: {
        glass: '20px',
      }
    },
  },
  plugins: [],
}
