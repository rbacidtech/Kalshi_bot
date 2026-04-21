import { create } from 'zustand'
import { useEffect, useState } from 'react'
import { CheckCircle2, AlertTriangle, XCircle, Info, X } from 'lucide-react'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ToastItem {
  id: string
  message: string
  type: 'success' | 'warning' | 'error' | 'info'
  duration: number
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

interface ToastStore {
  toasts: ToastItem[]
  add: (t: ToastItem) => void
  remove: (id: string) => void
}

const useToastStore = create<ToastStore>((set) => ({
  toasts: [],
  add: (t) => set((s) => ({ toasts: [...s.toasts.slice(-4), t] })), // max 5
  remove: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}))

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useToast() {
  const add = useToastStore((s) => s.add)
  return {
    toast: (
      message: string,
      type: ToastItem['type'] = 'info',
      duration = 4000,
    ) => {
      add({ id: crypto.randomUUID(), message, type, duration })
    },
  }
}

// ---------------------------------------------------------------------------
// Per-type config
// ---------------------------------------------------------------------------

const TYPE_CONFIG = {
  success: {
    border: 'border-l-emerald-500',
    icon: <CheckCircle2 size={16} className="text-emerald-400 shrink-0" />,
  },
  warning: {
    border: 'border-l-amber-400',
    icon: <AlertTriangle size={16} className="text-amber-400 shrink-0" />,
  },
  error: {
    border: 'border-l-rose-500',
    icon: <XCircle size={16} className="text-rose-400 shrink-0" />,
  },
  info: {
    border: 'border-l-blue-400',
    icon: <Info size={16} className="text-blue-400 shrink-0" />,
  },
} as const

// ---------------------------------------------------------------------------
// ToastCard
// ---------------------------------------------------------------------------

function ToastCard({ toast }: { toast: ToastItem }) {
  const remove = useToastStore((s) => s.remove)
  const [visible, setVisible] = useState(false)

  // Trigger fade-in on mount
  useEffect(() => {
    // Defer one frame so the CSS transition fires
    const raf = requestAnimationFrame(() => setVisible(true))
    return () => cancelAnimationFrame(raf)
  }, [])

  // Auto-dismiss
  useEffect(() => {
    const timer = setTimeout(() => remove(toast.id), toast.duration)
    return () => clearTimeout(timer)
  }, [toast.id, toast.duration, remove])

  const { border, icon } = TYPE_CONFIG[toast.type]

  return (
    <div
      className={[
        'w-80 rounded-xl border border-l-4 shadow-lg bg-surface-1',
        'flex items-start gap-3 px-4 py-3',
        'transition-all duration-300',
        border,
        visible ? 'opacity-100 translate-x-0' : 'opacity-0 translate-x-full',
      ].join(' ')}
    >
      {/* Type icon */}
      <span className="mt-0.5">{icon}</span>

      {/* Message */}
      <p className="flex-1 text-sm text-slate-200 leading-snug line-clamp-2 break-words">
        {toast.message}
      </p>

      {/* Dismiss button */}
      <button
        onClick={() => remove(toast.id)}
        className="mt-0.5 shrink-0 text-slate-400 hover:text-slate-200 transition-colors"
        aria-label="Dismiss"
      >
        <X size={14} />
      </button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Toaster
// ---------------------------------------------------------------------------

export function Toaster() {
  const toasts = useToastStore((s) => s.toasts)

  return (
    <div
      className="fixed bottom-5 right-5 z-[100] flex flex-col gap-2 items-end"
      aria-live="polite"
      aria-label="Notifications"
    >
      {toasts.map((t) => (
        <ToastCard key={t.id} toast={t} />
      ))}
    </div>
  )
}
