import { NavLink } from 'react-router-dom'
import { ChartBar, Key, CreditCard, Shield, SlidersHorizontal, TrendingUp } from 'lucide-react'
import { useAuth } from '../lib/auth'

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

export default function Sidebar() {
  const { user } = useAuth()

  const navItems: NavItem[] = [
    { to: '/dashboard',    icon: <ChartBar size={16} />,     label: 'Portfolio'    },
    { to: '/performance',  icon: <TrendingUp size={16} />,  label: 'Performance'  },
    { to: '/keys',         icon: <Key size={16} />,          label: 'API Keys'     },
    { to: '/subscription', icon: <CreditCard size={16} />,  label: 'Subscription' },
  ]

  if (user?.is_admin) {
    navItems.push({ to: '/controls', icon: <SlidersHorizontal size={16} />, label: 'Controls' })
    navItems.push({ to: '/admin',    icon: <Shield size={16} />,            label: 'Admin' })
  }

  const avatarLetter = user?.email?.[0]?.toUpperCase() ?? '?'
  const tier         = user?.tier ?? 'free'

  return (
    <aside className="flex flex-col w-60 shrink-0 h-screen bg-surface-1 border-r border-border">
      {/* Logo */}
      <div className="flex items-center gap-3 px-5 py-5 border-b border-border">
        {/* Lightning bolt SVG */}
        <svg
          width="28"
          height="28"
          viewBox="0 0 28 28"
          fill="none"
          xmlns="http://www.w3.org/2000/svg"
          aria-hidden="true"
        >
          <rect width="28" height="28" rx="7" fill="#3b82f6" fillOpacity="0.15" />
          <path
            d="M16 4L8 15.5H14L12 24L20 12.5H14L16 4Z"
            fill="#3b82f6"
            stroke="#3b82f6"
            strokeWidth="0.5"
            strokeLinejoin="round"
          />
        </svg>

        <div className="leading-tight">
          <p className="text-slate-100 font-semibold text-sm tracking-wide">EdgePulse</p>
          <p className="text-accent-blue text-[10px] font-medium tracking-widest uppercase">
            Trading
          </p>
        </div>
      </div>

      {/* Navigation */}
      <nav className="flex-1 px-3 py-4 space-y-0.5 overflow-y-auto">
        {navItems.map(({ to, icon, label }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) =>
              [
                'flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors duration-150',
                isActive
                  ? 'bg-accent-blue/10 text-accent-blue border-l-2 border-accent-blue pl-[10px]'
                  : 'text-muted hover:text-slate-200 hover:bg-surface-2 border-l-2 border-transparent pl-[10px]',
              ].join(' ')
            }
          >
            {icon}
            {label}
          </NavLink>
        ))}
      </nav>

      {/* User info block */}
      <div className="px-3 pb-4 pt-3 border-t border-border">
        <div className="flex items-center gap-3 px-2 py-2.5 rounded-lg bg-surface-2">
          {/* Avatar circle */}
          <div className="w-7 h-7 rounded-full bg-accent-blue/20 border border-accent-blue/30 flex items-center justify-center shrink-0">
            <span className="text-accent-blue text-xs font-semibold">{avatarLetter}</span>
          </div>

          <div className="min-w-0 flex-1">
            <p
              className="text-slate-300 text-xs font-medium truncate leading-tight"
              title={user?.email}
            >
              {user?.email ?? '—'}
            </p>
            <span className={`mt-0.5 ${TIER_BADGE[tier] ?? 'badge-muted'} text-[10px] py-0`}>
              {TIER_LABEL[tier] ?? tier}
            </span>
          </div>
        </div>
      </div>
    </aside>
  )
}
