import { NavLink } from 'react-router-dom'
import { ChartBar, Key, CreditCard, Shield, SlidersHorizontal, TrendingUp, LogOut, X, Brain, Bell } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { useAuth } from '../lib/auth'
import { positions as positionsApi, controls, performance as perfApi } from '../lib/api'

const TIER_BADGE: Record<string, string> = {
  free:          'badge-muted',
  starter:       'badge-blue',
  pro:           'badge-success',
  institutional: 'badge-success',
}

const TIER_LABEL: Record<string, string> = {
  free:          'Free',
  starter:       'Starter',
  pro:           'Pro',
  institutional: 'Institutional',
}

interface NavItem {
  to:    string
  icon:  React.ReactNode
  label: string
}

interface SidebarProps {
  onClose: () => void
}

export default function Sidebar({ onClose }: SidebarProps) {
  const { user, logout } = useAuth()

  // Read from cache — DashboardPage fetches these; no extra network calls
  const { data: portfolio } = useQuery({
    queryKey: ['portfolio'],
    queryFn:  () => positionsApi.portfolio().then(r => r.data),
    staleTime: 30_000,
    refetchInterval: 30_000,
  })
  const { data: botStatus } = useQuery({
    queryKey: ['bot-status'],
    queryFn:  () => controls.getStatus().then(r => r.data),
    staleTime: 15_000,
    refetchInterval: 15_000,
    enabled: !!user?.is_admin,
  })
  const { data: botCfg } = useQuery({
    queryKey: ['bot-config'],
    queryFn:  () => controls.getConfig().then(r => r.data),
    staleTime: 30_000,
    enabled: !!user?.is_admin,
  })
  const { data: perfData } = useQuery({
    queryKey: ['performance', 30],
    queryFn:  () => perfApi.summary(30).then(r => r.data),
    staleTime: 60_000,
  })

  // Drawdown alert: session loss > 50% of daily limit
  const ddLimit    = (botCfg as any)?.daily_drawdown_limit ?? 0.10
  const sessionPnl = (botStatus as any)?.session_pnl ?? 0
  const balCents   = (botStatus as any)?.balance_cents ?? 1
  const ddPct      = Math.abs(Math.min(0, sessionPnl)) / balCents
  const hasDDAlert = !!user?.is_admin && ddPct > ddLimit * 0.5

  // Aging position alert: any position older than 2× avg hold time
  const avgHold  = ((perfData as any)?.avg_hold_time_hours ?? 0) * 3_600_000
  const hasAging = avgHold > 0 && ((portfolio as any)?.positions ?? []).some((p: any) => {
    if (!p.entered_at) return false
    return (Date.now() - new Date(p.entered_at).getTime()) > 2 * avgHold
  })

  const hasAlert   = hasDDAlert || hasAging
  const alertColor = hasDDAlert ? 'bg-rose-500' : 'bg-amber-400'

  const navItems: NavItem[] = [
    { to: '/dashboard',    icon: <ChartBar size={16} />,          label: 'Portfolio'    },
    { to: '/performance',  icon: <TrendingUp size={16} />,        label: 'Performance'  },
    { to: '/keys',         icon: <Key size={16} />,               label: 'API Keys'     },
    { to: '/subscription', icon: <CreditCard size={16} />,        label: 'Subscription' },
  ]

  if (user?.is_admin) {
    navItems.push({ to: '/notifications', icon: <Bell size={16} />,               label: 'Notifications' })
    navItems.push({ to: '/advisor',       icon: <Brain size={16} />,              label: 'Advisor'  })
    navItems.push({ to: '/controls',      icon: <SlidersHorizontal size={16} />,  label: 'Controls' })
    navItems.push({ to: '/admin',         icon: <Shield size={16} />,             label: 'Admin'    })
  }

  const avatarLetter = user?.email?.[0]?.toUpperCase() ?? '?'
  const tier         = user?.tier ?? 'free'

  return (
    <aside className="flex flex-col w-60 shrink-0 h-screen bg-surface-1 border-r border-border">

      {/* ── Logo ─────────────────────────────────────────────────────────── */}
      <div className="flex items-center gap-3 px-5 py-5 border-b border-border relative">
        {/* Rocket icon in gradient circle */}
        <div
          className="w-9 h-9 rounded-xl flex items-center justify-center shrink-0 text-xl"
          style={{
            background: 'linear-gradient(135deg, #3b82f6 0%, #6366f1 100%)',
            boxShadow: '0 0 16px rgba(99,102,241,0.45)',
          }}
        >
          🚀
        </div>

        <div className="leading-tight min-w-0 flex-1">
          <p className="text-slate-100 font-bold text-sm tracking-wide">EdgePulse</p>
          <div className="flex items-center gap-1.5 mt-0.5">
            <span className="relative flex h-1.5 w-1.5">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
              <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-emerald-500" />
            </span>
            <p className="text-emerald-400 text-[10px] font-semibold tracking-widest uppercase">Live</p>
          </div>
        </div>
        <button
          onClick={onClose}
          className="text-slate-500 hover:text-slate-300 transition-colors p-1 rounded"
          title="Collapse sidebar"
        >
          <X size={15} />
        </button>
      </div>

      {/* ── Navigation ───────────────────────────────────────────────────── */}
      <nav className="flex-1 px-3 py-4 space-y-0.5 overflow-y-auto">
        {navItems.map(({ to, icon, label }) => (
          <NavLink
            key={to}
            to={to}
            onClick={onClose}
            className={({ isActive }) => [
              'flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-all duration-150',
              isActive
                ? 'bg-gradient-to-r from-blue-600/20 to-indigo-600/10 text-white border-l-2 border-blue-400 pl-[10px] shadow-[inset_0_0_12px_rgba(96,165,250,0.08)]'
                : 'text-muted hover:text-slate-200 hover:bg-surface-2 border-l-2 border-transparent pl-[10px]',
            ].join(' ')}
          >
            <span className="relative shrink-0">
              {icon}
              {to === '/dashboard' && hasAlert && (
                <span className={`absolute -top-1 -right-1 w-2 h-2 rounded-full ${alertColor} ring-2 ring-surface-1`}>
                  <span className={`absolute inset-0 rounded-full ${alertColor} animate-ping opacity-75`} />
                </span>
              )}
            </span>
            {label}
          </NavLink>
        ))}
      </nav>

      {/* ── User block ───────────────────────────────────────────────────── */}
      <div className="px-3 pb-4 pt-3 border-t border-border space-y-2">
        <div className="flex items-center gap-3 px-2 py-2.5 rounded-lg bg-surface-2">
          <div className="w-7 h-7 rounded-full bg-gradient-to-br from-blue-500/30 to-indigo-500/30 border border-blue-500/30 flex items-center justify-center shrink-0">
            <span className="text-accent-blue text-xs font-bold">{avatarLetter}</span>
          </div>
          <div className="min-w-0 flex-1">
            <p className="text-slate-300 text-xs font-medium truncate leading-tight" title={user?.email}>
              {user?.email ?? '—'}
            </p>
            <span className={`mt-0.5 ${TIER_BADGE[tier] ?? 'badge-muted'} text-[10px] py-0`}>
              {TIER_LABEL[tier] ?? tier}
            </span>
          </div>
        </div>

        <button
          onClick={() => logout()}
          className="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-slate-500 text-xs font-medium
                     hover:text-rose-400 hover:bg-rose-500/5 transition-colors duration-150"
        >
          <LogOut size={13} />
          Sign out
        </button>
      </div>
    </aside>
  )
}
