import axios from 'axios'

const BASE = import.meta.env.VITE_API_URL ?? window.location.origin

export const api = axios.create({ baseURL: BASE, withCredentials: true })

// Inject Bearer token on every request
api.interceptors.request.use(cfg => {
  const token = localStorage.getItem('ep_access')
  if (token) cfg.headers.Authorization = `Bearer ${token}`
  return cfg
})

// Auto-refresh on 401
api.interceptors.response.use(
  r => r,
  async err => {
    const original = err.config
    if (err.response?.status === 401 && !original._retry) {
      original._retry = true
      try {
        const refresh = localStorage.getItem('ep_refresh')
        if (!refresh) throw new Error('no refresh token')
        const { data } = await axios.post(`${BASE}/auth/refresh`, { refresh_token: refresh })
        localStorage.setItem('ep_access', data.access_token)
        original.headers.Authorization = `Bearer ${data.access_token}`
        return api(original)
      } catch {
        localStorage.removeItem('ep_access')
        localStorage.removeItem('ep_refresh')
        if (window.location.pathname !== '/login') {
          window.location.href = '/login'
        }
      }
    }
    return Promise.reject(err)
  }
)

// ── Typed API calls ──────────────────────────────────────────────────────────

export const auth = {
  login:    (email: string, password: string) =>
    api.post('/auth/login', { email, password }),
  register: (email: string, password: string) =>
    api.post('/auth/register', { email, password }),
  me:       () => api.get('/auth/me'),
  logout:   () => api.post('/auth/logout'),
}

export const positions = {
  portfolio:        () => api.get('/positions'),
  prices:           () => api.get('/positions/prices'),
  balance:          () => api.get('/positions/balance'),
  coinbaseBalance:  () => api.get('/positions/coinbase'),
}

export const keys = {
  list:   () => api.get('/keys'),
  store:  (exchange: string, key_id: string, private_key: string) =>
    api.post('/keys', { exchange, key_id, private_key }),
  remove: (exchange: string) => api.delete(`/keys/${exchange}`),
  verify: (exchange: string) => api.get(`/keys/${exchange}/verify`),
}

export const subscriptions = {
  me:    () => api.get('/subscriptions/me'),
  tiers: () => api.get('/subscriptions/tiers'),
}

export const performance = {
  summary: (days: number) => api.get('/performance', { params: { days } }),
  history: (hours = 24)   => api.get('/performance/history', { params: { hours } }),
}

export const controls = {
  getConfig:   () => api.get('/controls/config'),
  patchConfig: (cfg: Record<string, unknown>) => api.patch('/controls/config', cfg),
  getStatus:   () => api.get('/controls/status'),
  aiSuggest:   (config: Record<string, unknown>, question: string, perf?: Record<string, unknown>) =>
    api.post('/controls/ai-suggest', { config, question, performance: perf ?? {} }),
}

export const admin = {
  users:     (page = 1, per_page = 20) => api.get('/admin/users', { params: { page, per_page } }),
  user:      (id: string) => api.get(`/admin/users/${id}`),
  updateUser:(id: string, patch: Record<string, unknown>) => api.patch(`/admin/users/${id}`, patch),
  deleteUser:(id: string) => api.delete(`/admin/users/${id}`),
  audit:     (id: string, limit = 50) => api.get(`/admin/users/${id}/audit`, { params: { limit } }),
  stats:     () => api.get('/admin/stats'),
}
