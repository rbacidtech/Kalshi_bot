import { LogOut } from 'lucide-react'
import { useAuth } from '../lib/auth'

interface TopbarProps {
  title?: string
}

export default function Topbar({ title = '' }: TopbarProps) {
  const { user, logout } = useAuth()

  return (
    <header className="sticky top-0 z-30 h-14 flex items-center justify-between px-6 bg-surface-0/80 backdrop-blur border-b border-border">
      {/* Left: page title */}
      <h1 className="font-semibold text-slate-100 text-base leading-none">
        {title}
      </h1>

      {/* Right: live indicator + user + logout */}
      <div className="flex items-center gap-4">
        {/* Live indicator */}
        <div className="flex items-center gap-1.5">
          <span className="w-2 h-2 rounded-full bg-success animate-pulse2 inline-block" />
          <span className="text-success text-xs font-semibold tracking-widest uppercase">
            Live
          </span>
        </div>

        {/* User email */}
        {user?.email && (
          <span
            className="text-muted text-sm hidden sm:block truncate max-w-[200px]"
            title={user.email}
          >
            {user.email}
          </span>
        )}

        {/* Logout button */}
        <button
          onClick={() => logout()}
          className="btn-ghost text-xs px-2.5 py-1.5"
          title="Sign out"
        >
          <LogOut size={14} />
          <span className="hidden sm:inline">Logout</span>
        </button>
      </div>
    </header>
  )
}
