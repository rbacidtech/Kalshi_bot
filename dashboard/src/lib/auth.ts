import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { auth as authApi } from './api'

export interface User {
  id: string
  email: string
  tier: 'free' | 'starter' | 'pro' | 'institutional'
  is_active: boolean
  is_admin: boolean
  created_at: string
  last_login_at: string | null
}

interface AuthState {
  user: User | null
  loading: boolean
  login:    (email: string, password: string) => Promise<void>
  register: (email: string, password: string) => Promise<void>
  logout:   () => Promise<void>
  fetchMe:  () => Promise<void>
}

export const useAuth = create<AuthState>()(
  persist(
    (set) => ({
      user: null,
      loading: false,

      login: async (email, password) => {
        const { data } = await authApi.login(email, password)
        localStorage.setItem('ep_access', data.access_token)
        if (data.refresh_token) localStorage.setItem('ep_refresh', data.refresh_token)
        const { data: me } = await authApi.me()
        set({ user: me })
      },

      register: async (email, password) => {
        await authApi.register(email, password)
        const { data } = await authApi.login(email, password)
        localStorage.setItem('ep_access', data.access_token)
        if (data.refresh_token) localStorage.setItem('ep_refresh', data.refresh_token)
        const { data: me } = await authApi.me()
        set({ user: me })
      },

      logout: async () => {
        try { await authApi.logout() } catch { /* ignore */ }
        localStorage.removeItem('ep_access')
        localStorage.removeItem('ep_refresh')
        set({ user: null })
      },

      fetchMe: async () => {
        set({ loading: true })
        try {
          const { data } = await authApi.me()
          set({ user: data, loading: false })
        } catch {
          set({ user: null, loading: false })
        }
      },
    }),
    { name: 'ep-auth', partialize: state => ({ user: state.user }) }
  )
)
