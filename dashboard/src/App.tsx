import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { useAuth } from './lib/auth'
import { useEffect, Component, type ReactNode } from 'react'

class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null }
  static getDerivedStateFromError(error: Error) { return { error } }
  render() {
    if (this.state.error) {
      return (
        <div style={{ padding: 32, color: '#f87171', fontFamily: 'monospace', background: '#0a0f1e', minHeight: '100vh' }}>
          <p style={{ fontWeight: 'bold', fontSize: 16 }}>Page crashed — check browser console</p>
          <pre style={{ marginTop: 12, fontSize: 12, opacity: 0.8, whiteSpace: 'pre-wrap' }}>
            {(this.state.error as Error).message}
          </pre>
        </div>
      )
    }
    return this.props.children
  }
}
import Layout from './components/Layout'
import LoginPage from './pages/LoginPage'
import RegisterPage from './pages/RegisterPage'
import DashboardPage from './pages/DashboardPage'
import KeysPage from './pages/KeysPage'
import SubscriptionPage from './pages/SubscriptionPage'
import AdminPage from './pages/AdminPage'
import ControlsPage from './pages/ControlsPage'
import PerformancePage from './pages/PerformancePage'

const Spinner = () => (
  <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', background: '#0a0f1e' }}>
    <div style={{ width: 32, height: 32, border: '3px solid #1e3a5f', borderTop: '3px solid #60a5fa', borderRadius: '50%', animation: 'spin 0.8s linear infinite' }} />
    <style>{`@keyframes spin { to { transform: rotate(360deg) } }`}</style>
  </div>
)

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth()
  const hasToken = !!localStorage.getItem('ep_access')
  if (loading || (hasToken && !user)) return <Spinner />
  if (!user) return <Navigate to="/login" replace />
  return <>{children}</>
}

function RequireAdmin({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth()
  const hasToken = !!localStorage.getItem('ep_access')
  if (loading || (hasToken && !user)) return <Spinner />
  if (!user) return <Navigate to="/login" replace />
  if (!user.is_admin) return <Navigate to="/dashboard" replace />
  return <>{children}</>
}

export default function App() {
  const { fetchMe } = useAuth()
  useEffect(() => {
    try {
      const params = new URLSearchParams(window.location.search)
      const msToken = params.get('ms_token')
      if (msToken) {
        localStorage.setItem('ep_access', msToken)
        window.history.replaceState({}, '', '/dashboard')
      }
    } catch (_) {}
    fetchMe().catch(() => {})
  }, [])

  return (
    <ErrorBoundary>
    <BrowserRouter>
      <Routes>
        <Route path="/login"    element={<LoginPage />} />
        <Route path="/register" element={<RegisterPage />} />
        <Route element={<RequireAuth><Layout /></RequireAuth>}>
          <Route path="/dashboard"    element={<DashboardPage />} />
          <Route path="/performance"  element={<PerformancePage />} />
          <Route path="/keys"         element={<KeysPage />} />
          <Route path="/subscription" element={<SubscriptionPage />} />
          <Route path="/controls"     element={<RequireAdmin><ControlsPage /></RequireAdmin>} />
          <Route path="/admin"        element={<RequireAdmin><AdminPage /></RequireAdmin>} />
          <Route path="/"             element={<Navigate to="/dashboard" replace />} />
        </Route>
        <Route path="*" element={<Navigate to="/dashboard" replace />} />
      </Routes>
    </BrowserRouter>
    </ErrorBoundary>
  )
}
