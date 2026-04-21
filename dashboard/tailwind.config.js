/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        surface: {
          0: '#0a0f1e',
          1: '#131929',
          2: '#1a2238',
          3: '#212b45',
        },
        border: '#2d3f5f',
        accent: {
          blue: '#60a5fa',
          cyan: '#22d3ee',
        },
        success: '#34d399',
        danger:  '#f87171',
        warning: '#fbbf24',
        muted:   '#94a3b8',
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'Fira Code', 'monospace'],
      },
      backgroundImage: {
        'gradient-radial': 'radial-gradient(var(--tw-gradient-stops))',
      },
      keyframes: {
        fadeIn:   { from: { opacity: 0, transform: 'translateY(6px)' }, to: { opacity: 1, transform: 'translateY(0)' } },
        pulse2:   { '0%,100%': { opacity: 1 }, '50%': { opacity: 0.4 } },
        slideIn:  { from: { opacity: 0, transform: 'translateX(100%)' }, to: { opacity: 1, transform: 'translateX(0)' } },
      },
      animation: {
        fadeIn:  'fadeIn 0.25s ease-out',
        pulse2:  'pulse2 2s ease-in-out infinite',
        slideIn: 'slideIn 0.25s ease-out',
      },
    },
  },
  plugins: [],
}
