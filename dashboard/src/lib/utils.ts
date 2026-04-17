import { type ClassValue, clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function formatCents(cents: number | null | undefined, opts?: { sign?: boolean }): string {
  if (cents == null) return '—'
  const val = cents / 100
  const formatted = Math.abs(val).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  if (opts?.sign) {
    return val >= 0 ? `+$${formatted}` : `-$${formatted}`
  }
  return `$${formatted}`
}

export function formatDate(iso: string | null | undefined, format: 'short' | 'datetime' = 'short'): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (format === 'datetime') {
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
  }
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return 'Never'
  const seconds = Math.floor((Date.now() - new Date(iso).getTime()) / 1000)
  if (seconds < 60) return 'Just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

export const TIER_COLORS: Record<string, string> = {
  free:          'badge-muted',
  starter:       'badge-blue',
  pro:           'badge-success',
  institutional: 'bg-violet-900/30 text-violet-300 inline-flex items-center px-2 py-0.5 rounded text-xs font-medium',
}

export const TIER_LABELS: Record<string, string> = {
  free: 'Free', starter: 'Starter', pro: 'Pro', institutional: 'Institutional',
}
