import { useState, FormEvent } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { Eye, EyeOff, Loader2 } from 'lucide-react'
import { useAuth } from '../lib/auth'

export default function LoginPage() {
  const navigate  = useNavigate()
  const { login } = useAuth()

  const [email,    setEmail]    = useState('')
  const [password, setPassword] = useState('')
  const [showPass, setShowPass] = useState(false)
  const [loading,  setLoading]  = useState(false)
  const [error,    setError]    = useState<string | null>(null)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setLoading(true)
    try {
      await login(email, password)
      navigate('/dashboard', { replace: true })
    } catch (err: unknown) {
      const axiosErr = err as { response?: { data?: { detail?: string } } }
      setError(axiosErr.response?.data?.detail ?? 'Login failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center px-4 py-10" style={{ background: '#0a0f1e' }}>

      {/* Subtle background glow */}
      <div className="pointer-events-none fixed inset-0 overflow-hidden" aria-hidden>
        <div className="absolute top-[-20%] left-[50%] -translate-x-1/2 w-[600px] h-[600px] rounded-full opacity-20"
          style={{ background: 'radial-gradient(circle, #3b82f6 0%, transparent 70%)' }} />
      </div>

      <div className="relative w-full max-w-sm animate-fadeIn">

        {/* Brand */}
        <div className="flex flex-col items-center mb-8">
          <div
            className="w-14 h-14 rounded-2xl flex items-center justify-center text-2xl mb-4"
            style={{ background: 'linear-gradient(135deg, #3b82f6 0%, #6366f1 100%)', boxShadow: '0 0 32px rgba(99,102,241,0.5)' }}
          >
            🚀
          </div>
          <h1 className="text-2xl font-bold text-white tracking-tight">EdgePulse</h1>
          <p className="text-sm text-slate-500 mt-1">Prediction market trading intelligence</p>
        </div>

        {/* Card */}
        <div
          className="bg-surface-1 border border-border rounded-2xl p-7"
          style={{ borderTopColor: '#3b82f6', borderTopWidth: 3, boxShadow: '0 24px 48px rgba(0,0,0,0.4)' }}
        >

          {/* Microsoft */}
          <a
            href="/auth/microsoft/login"
            className="w-full flex items-center justify-center gap-2.5 px-4 py-2.5 rounded-xl border border-border
                       bg-surface-2 text-slate-200 text-sm font-semibold hover:bg-surface-3 hover:border-slate-600
                       transition-all duration-150 mb-5"
          >
            <svg width="18" height="18" viewBox="0 0 21 21" fill="none">
              <rect x="1"  y="1"  width="9" height="9" fill="#f25022" />
              <rect x="11" y="1"  width="9" height="9" fill="#7fba00" />
              <rect x="1"  y="11" width="9" height="9" fill="#00a4ef" />
              <rect x="11" y="11" width="9" height="9" fill="#ffb900" />
            </svg>
            Continue with Microsoft
          </a>

          {/* Divider */}
          <div className="flex items-center gap-3 mb-5">
            <div className="flex-1 h-px bg-border" />
            <span className="text-xs text-slate-600 font-medium">or email</span>
            <div className="flex-1 h-px bg-border" />
          </div>

          {/* Form */}
          <form onSubmit={handleSubmit} noValidate className="space-y-4">

            <div>
              <label className="label" htmlFor="email">Email address</label>
              <input
                id="email"
                type="email"
                autoComplete="email"
                required
                className="input"
                placeholder="you@example.com"
                value={email}
                onChange={e => setEmail(e.target.value)}
              />
            </div>

            <div>
              <label className="label" htmlFor="password">Password</label>
              <div className="relative">
                <input
                  id="password"
                  type={showPass ? 'text' : 'password'}
                  autoComplete="current-password"
                  required
                  className="input pr-10"
                  placeholder="••••••••"
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                />
                <button
                  type="button"
                  onClick={() => setShowPass(v => !v)}
                  className="absolute inset-y-0 right-0 flex items-center px-3 text-slate-500 hover:text-slate-300 transition-colors"
                  tabIndex={-1}
                >
                  {showPass ? <EyeOff size={16} /> : <Eye size={16} />}
                </button>
              </div>
            </div>

            {error && (
              <div className="rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
                {error}
              </div>
            )}

            <button
              type="submit"
              disabled={loading}
              className="btn-primary w-full justify-center py-2.5 text-sm font-semibold mt-1"
            >
              {loading && <Loader2 size={15} className="animate-spin" />}
              {loading ? 'Signing in…' : 'Sign in'}
            </button>
          </form>

          <p className="mt-5 text-center text-xs text-slate-600">
            Don't have an account?{' '}
            <Link to="/register" className="text-accent-blue hover:text-blue-400 font-semibold transition-colors">
              Sign up
            </Link>
          </p>
        </div>
      </div>
    </div>
  )
}
