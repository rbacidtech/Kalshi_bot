import { useState, FormEvent } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { Eye, EyeOff, Loader2, CheckCircle2, Circle } from 'lucide-react'
import { useAuth } from '../lib/auth'

/* ── Password requirement helpers ─────────────────────────────────────── */
interface Requirement {
  label: string
  test:  (pw: string) => boolean
}

const REQUIREMENTS: Requirement[] = [
  { label: '8+ characters',         test: pw => pw.length >= 8 },
  { label: 'Uppercase letter',       test: pw => /[A-Z]/.test(pw) },
  { label: 'Number',                 test: pw => /[0-9]/.test(pw) },
  { label: 'Special character',      test: pw => /[^A-Za-z0-9]/.test(pw) },
]

function RequirementItem({ label, met }: { label: string; met: boolean }) {
  return (
    <li className={`flex items-center gap-1.5 text-xs transition-colors ${met ? 'text-success' : 'text-muted'}`}>
      {met
        ? <CheckCircle2 size={12} strokeWidth={2.5} className="shrink-0" />
        : <Circle       size={12} strokeWidth={2}   className="shrink-0 opacity-50" />
      }
      {label}
    </li>
  )
}

/* ── Page ─────────────────────────────────────────────────────────────── */
export default function RegisterPage() {
  const navigate      = useNavigate()
  const { register }  = useAuth()

  const [email,       setEmail]       = useState('')
  const [password,    setPassword]    = useState('')
  const [confirm,     setConfirm]     = useState('')
  const [showPass,    setShowPass]    = useState(false)
  const [showConfirm, setShowConfirm] = useState(false)
  const [loading,     setLoading]     = useState(false)
  const [error,       setError]       = useState<string | null>(null)

  /* Only show requirement hints once the user has started typing */
  const showHints = password.length > 0
  const allMet    = REQUIREMENTS.every(r => r.test(password))
  const mismatch  = confirm.length > 0 && confirm !== password

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)

    if (!allMet) {
      setError('Password does not meet all requirements.')
      return
    }
    if (password !== confirm) {
      setError('Passwords do not match.')
      return
    }

    setLoading(true)
    try {
      await register(email, password)
      navigate('/dashboard', { replace: true })
    } catch (err: unknown) {
      const axiosErr = err as { response?: { data?: { detail?: string } } }
      setError(axiosErr.response?.data?.detail ?? 'Registration failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div
      className="min-h-screen flex items-center justify-center bg-surface-0 px-4 py-10"
      style={{
        background: 'radial-gradient(circle at 50% 30%, rgba(59,130,246,0.05) 0%, transparent 60%), #020817',
      }}
    >
      <div className="w-full max-w-md animate-fadeIn">
        {/* Card */}
        <div className="card p-8 shadow-2xl">
          {/* Logo + Heading */}
          <div className="flex flex-col items-center mb-8">
            <div className="flex items-center gap-3 mb-3">
              <svg
                width="32"
                height="32"
                viewBox="0 0 32 32"
                fill="none"
                xmlns="http://www.w3.org/2000/svg"
                aria-hidden="true"
              >
                <rect width="32" height="32" rx="8" fill="#3b82f6" fillOpacity="0.15" />
                <polygon
                  points="18,4 10,18 15,18 14,28 22,14 17,14"
                  fill="#3b82f6"
                />
              </svg>
              <span className="text-2xl font-semibold tracking-tight text-slate-100">
                EdgePulse
              </span>
            </div>
            <h1 className="text-lg font-semibold text-slate-100 mb-1">
              Create your account
            </h1>
            <p className="text-sm text-muted text-center">
              Start trading with EdgePulse signals
            </p>
          </div>

          {/* Form */}
          <form onSubmit={handleSubmit} noValidate className="space-y-5">
            {/* Email */}
            <div>
              <label htmlFor="email" className="label">
                Email address
              </label>
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

            {/* Password */}
            <div>
              <label htmlFor="password" className="label">
                Password
              </label>
              <div className="relative">
                <input
                  id="password"
                  type={showPass ? 'text' : 'password'}
                  autoComplete="new-password"
                  required
                  className="input pr-10"
                  placeholder="••••••••"
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                />
                <button
                  type="button"
                  aria-label={showPass ? 'Hide password' : 'Show password'}
                  onClick={() => setShowPass(v => !v)}
                  className="absolute inset-y-0 right-0 flex items-center px-3 text-muted hover:text-slate-300 transition-colors"
                  tabIndex={-1}
                >
                  {showPass
                    ? <EyeOff size={16} strokeWidth={1.75} />
                    : <Eye    size={16} strokeWidth={1.75} />
                  }
                </button>
              </div>

              {/* Requirement hints */}
              {showHints && (
                <ul className="mt-2.5 grid grid-cols-2 gap-x-4 gap-y-1.5 pl-0.5">
                  {REQUIREMENTS.map(r => (
                    <RequirementItem key={r.label} label={r.label} met={r.test(password)} />
                  ))}
                </ul>
              )}
            </div>

            {/* Confirm Password */}
            <div>
              <label htmlFor="confirm" className="label">
                Confirm password
              </label>
              <div className="relative">
                <input
                  id="confirm"
                  type={showConfirm ? 'text' : 'password'}
                  autoComplete="new-password"
                  required
                  className={`input pr-10 ${mismatch ? 'border-danger focus:ring-danger focus:border-danger' : ''}`}
                  placeholder="••••••••"
                  value={confirm}
                  onChange={e => setConfirm(e.target.value)}
                />
                <button
                  type="button"
                  aria-label={showConfirm ? 'Hide password' : 'Show password'}
                  onClick={() => setShowConfirm(v => !v)}
                  className="absolute inset-y-0 right-0 flex items-center px-3 text-muted hover:text-slate-300 transition-colors"
                  tabIndex={-1}
                >
                  {showConfirm
                    ? <EyeOff size={16} strokeWidth={1.75} />
                    : <Eye    size={16} strokeWidth={1.75} />
                  }
                </button>
              </div>
              {mismatch && (
                <p className="mt-1.5 text-xs text-danger">Passwords do not match.</p>
              )}
            </div>

            {/* Error alert */}
            {error && (
              <div
                role="alert"
                className="flex items-start gap-2.5 rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger"
              >
                <svg
                  width="16"
                  height="16"
                  viewBox="0 0 16 16"
                  fill="currentColor"
                  className="mt-0.5 shrink-0"
                  aria-hidden="true"
                >
                  <path d="M8 1a7 7 0 1 0 0 14A7 7 0 0 0 8 1zm0 3.5a.75.75 0 0 1 .75.75v3a.75.75 0 0 1-1.5 0v-3A.75.75 0 0 1 8 4.5zm0 6.75a.875.875 0 1 1 0-1.75.875.875 0 0 1 0 1.75z" />
                </svg>
                {error}
              </div>
            )}

            {/* Submit */}
            <button
              type="submit"
              disabled={loading}
              className="btn-primary w-full justify-center py-2.5 text-sm font-semibold"
            >
              {loading && (
                <Loader2 size={15} className="animate-spin" aria-hidden="true" />
              )}
              {loading ? 'Creating account…' : 'Create account'}
            </button>
          </form>

          {/* Footer link */}
          <p className="mt-6 text-center text-xs text-muted">
            Already have an account?{' '}
            <Link
              to="/login"
              className="text-accent-blue hover:text-blue-400 font-medium transition-colors"
            >
              Sign in
            </Link>
          </p>
        </div>
      </div>
    </div>
  )
}
