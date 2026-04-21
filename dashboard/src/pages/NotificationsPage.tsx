import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Bell, RefreshCw, TrendingUp, TrendingDown, AlertTriangle, Info, Activity, Zap, BarChart2 } from 'lucide-react'
import { notifications } from '../lib/api'

// ── Types ─────────────────────────────────────────────────────────────────────

interface Notification {
  id:           string
  ts:           string
  type:         string
  severity:     string
  message:      string
  ticker?:      string
  side?:        string
  contracts?:   number
  price_cents?: number
  entry_cents?: number
  current_cents?: number
  pnl_cents?:   number
  edge?:        number
  strategy?:    string
  mode?:        string
  reason?:      string
  name?:        string
  failure_count?: number
  trades?:      number
  win_rate?:    number
  open_positions?: number
}

type TypeFilter = 'all' | 'fill' | 'exit' | 'alert' | 'trade_alert' | 'circuit_breaker' | 'daily_summary'
type SeverityFilter = 'all' | 'info' | 'warning' | 'critical'

// ── Helpers ───────────────────────────────────────────────────────────────────

function relativeTime(iso: string): string {
  try {
    const diff = Date.now() - new Date(iso).getTime()
    const m    = Math.floor(diff / 60_000)
    if (m < 1)  return 'just now'
    if (m < 60) return `${m}m ago`
    const h = Math.floor(m / 60)
    if (h < 24) return `${h}h ago`
    return `${Math.floor(h / 24)}d ago`
  } catch {
    return '—'
  }
}

function typeIcon(type: string, severity: string) {
  const cls = severity === 'critical' ? 'text-red-400' : severity === 'warning' ? 'text-yellow-400' : 'text-blue-400'
  switch (type) {
    case 'fill':           return <TrendingUp   size={16} className="text-green-400" />
    case 'exit':           return <TrendingDown size={16} className="text-orange-400" />
    case 'alert':          return severity === 'critical' ? <AlertTriangle size={16} className="text-red-400" /> : <Info size={16} className={cls} />
    case 'trade_alert':    return <Activity     size={16} className="text-purple-400" />
    case 'circuit_breaker': return <Zap         size={16} className="text-yellow-400" />
    case 'daily_summary':  return <BarChart2    size={16} className="text-blue-400" />
    default:               return <Bell         size={16} className="text-gray-400" />
  }
}

function severityBadge(severity: string) {
  const map: Record<string, string> = {
    critical: 'bg-red-900/40 text-red-300 border-red-700',
    warning:  'bg-yellow-900/40 text-yellow-300 border-yellow-700',
    info:     'bg-blue-900/40 text-blue-300 border-blue-700',
  }
  return (
    <span className={`px-1.5 py-0.5 rounded border text-xs font-medium ${map[severity] ?? 'bg-gray-800 text-gray-400 border-gray-600'}`}>
      {severity}
    </span>
  )
}

function typeBadge(type: string) {
  const label = type.replace('_', ' ').toUpperCase()
  const map: Record<string, string> = {
    fill:            'bg-green-900/40 text-green-300 border-green-700',
    exit:            'bg-orange-900/40 text-orange-300 border-orange-700',
    alert:           'bg-gray-800 text-gray-300 border-gray-600',
    trade_alert:     'bg-purple-900/40 text-purple-300 border-purple-700',
    circuit_breaker: 'bg-yellow-900/40 text-yellow-300 border-yellow-700',
    daily_summary:   'bg-blue-900/40 text-blue-300 border-blue-700',
  }
  return (
    <span className={`px-1.5 py-0.5 rounded border text-xs font-medium ${map[type] ?? 'bg-gray-800 text-gray-400 border-gray-600'}`}>
      {label}
    </span>
  )
}

// ── Main component ────────────────────────────────────────────────────────────

export default function NotificationsPage() {
  const [typeFilter, setTypeFilter]       = useState<TypeFilter>('all')
  const [severityFilter, setSeverityFilter] = useState<SeverityFilter>('all')
  const [limit, setLimit]                 = useState(50)

  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ['notifications', typeFilter, severityFilter, limit],
    queryFn:  () => notifications.list(
      limit,
      typeFilter     !== 'all' ? typeFilter     : undefined,
      severityFilter !== 'all' ? severityFilter : undefined,
    ).then(r => r.data),
    refetchInterval: 15_000,
  })

  const items: Notification[] = data?.notifications ?? []

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Bell size={20} className="text-blue-400" />
          <h1 className="text-xl font-semibold text-white">Notifications</h1>
          {data?.count != null && (
            <span className="text-sm text-gray-400">({data.count})</span>
          )}
        </div>
        <button
          onClick={() => refetch()}
          disabled={isFetching}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded bg-gray-800 hover:bg-gray-700 text-sm text-gray-300 transition-colors disabled:opacity-50"
        >
          <RefreshCw size={14} className={isFetching ? 'animate-spin' : ''} />
          Refresh
        </button>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-3 items-center">
        {/* Type filter */}
        <div className="flex items-center gap-1.5">
          <span className="text-xs text-gray-400 uppercase tracking-wide">Type</span>
          {(['all', 'fill', 'exit', 'alert', 'trade_alert', 'circuit_breaker', 'daily_summary'] as TypeFilter[]).map(t => (
            <button
              key={t}
              onClick={() => setTypeFilter(t)}
              className={`px-2.5 py-1 rounded text-xs font-medium transition-colors ${
                typeFilter === t
                  ? 'bg-blue-600 text-white'
                  : 'bg-gray-800 text-gray-400 hover:bg-gray-700'
              }`}
            >
              {t === 'all' ? 'All' : t.replace('_', ' ')}
            </button>
          ))}
        </div>

        {/* Severity filter */}
        <div className="flex items-center gap-1.5">
          <span className="text-xs text-gray-400 uppercase tracking-wide">Severity</span>
          {(['all', 'info', 'warning', 'critical'] as SeverityFilter[]).map(s => (
            <button
              key={s}
              onClick={() => setSeverityFilter(s)}
              className={`px-2.5 py-1 rounded text-xs font-medium transition-colors ${
                severityFilter === s
                  ? 'bg-blue-600 text-white'
                  : 'bg-gray-800 text-gray-400 hover:bg-gray-700'
              }`}
            >
              {s === 'all' ? 'All' : s}
            </button>
          ))}
        </div>

        {/* Limit */}
        <div className="flex items-center gap-1.5 ml-auto">
          <span className="text-xs text-gray-400">Show</span>
          {[25, 50, 100, 200].map(n => (
            <button
              key={n}
              onClick={() => setLimit(n)}
              className={`px-2 py-1 rounded text-xs font-medium transition-colors ${
                limit === n
                  ? 'bg-gray-600 text-white'
                  : 'bg-gray-800 text-gray-500 hover:bg-gray-700'
              }`}
            >
              {n}
            </button>
          ))}
        </div>
      </div>

      {/* Content */}
      {isLoading ? (
        <div className="flex items-center justify-center h-48 text-gray-500">Loading...</div>
      ) : error ? (
        <div className="rounded-lg border border-red-700 bg-red-900/20 p-4 text-red-300 text-sm">
          Failed to load notifications. Check that the API is running and Redis is connected.
        </div>
      ) : items.length === 0 ? (
        <div className="flex flex-col items-center justify-center h-48 text-gray-500 gap-2">
          <Bell size={32} className="opacity-30" />
          <p className="text-sm">No notifications yet.</p>
          <p className="text-xs text-gray-600">Notifications appear here when trades execute, exits fire, or system alerts trigger.</p>
        </div>
      ) : (
        <div className="space-y-1.5">
          {items.map(n => (
            <div
              key={n.id}
              className={`rounded-lg border p-3 flex items-start gap-3 ${
                n.severity === 'critical'
                  ? 'border-red-800 bg-red-950/30'
                  : n.severity === 'warning'
                  ? 'border-yellow-800 bg-yellow-950/20'
                  : 'border-gray-700 bg-gray-900/50'
              }`}
            >
              {/* Icon */}
              <div className="mt-0.5 shrink-0">{typeIcon(n.type, n.severity)}</div>

              {/* Body */}
              <div className="flex-1 min-w-0">
                <div className="flex flex-wrap items-center gap-1.5 mb-1">
                  {typeBadge(n.type)}
                  {severityBadge(n.severity)}
                  {n.mode && (
                    <span className={`px-1.5 py-0.5 rounded border text-xs font-medium ${
                      n.mode === 'live'
                        ? 'bg-red-900/40 text-red-300 border-red-700'
                        : 'bg-gray-800 text-gray-400 border-gray-600'
                    }`}>
                      {n.mode}
                    </span>
                  )}
                  <span className="text-xs text-gray-500 ml-auto">{relativeTime(n.ts)}</span>
                </div>
                <p className="text-sm text-gray-200 font-medium">{n.message}</p>

                {/* Extra details for fill/exit */}
                {(n.type === 'fill' || n.type === 'exit') && n.ticker && (
                  <div className="mt-1 flex flex-wrap gap-3 text-xs text-gray-400">
                    {n.ticker      && <span>Ticker: <span className="text-gray-200">{n.ticker}</span></span>}
                    {n.side        && <span>Side: <span className="text-gray-200">{n.side.toUpperCase()}</span></span>}
                    {n.contracts   != null && <span>Qty: <span className="text-gray-200">×{n.contracts}</span></span>}
                    {n.edge        != null && <span>Edge: <span className="text-gray-200">{n.edge.toFixed(3)}</span></span>}
                    {n.pnl_cents   != null && (
                      <span>P&L: <span className={n.pnl_cents >= 0 ? 'text-green-400' : 'text-red-400'}>
                        {n.pnl_cents >= 0 ? '+' : ''}{(n.pnl_cents / 100).toFixed(2)}
                      </span></span>
                    )}
                    {n.reason      && <span>Reason: <span className="text-gray-200">{n.reason}</span></span>}
                    {n.strategy    && <span>Strategy: <span className="text-gray-200">{n.strategy}</span></span>}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
