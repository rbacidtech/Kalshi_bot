import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Brain, AlertTriangle, CheckCircle, Info, TrendingUp, TrendingDown,
  Minus, RefreshCw, Zap, Clock,
} from 'lucide-react'
import { advisor } from '../lib/api'

// ── Types ─────────────────────────────────────────────────────────────────────

interface AdvisorAlert {
  id?:          string
  ts:           string
  severity:     'info' | 'warning' | 'critical'
  category:     string
  title:        string
  message:      string
  action?:      string | null
  recommended_action?: string | null
  auto_applied: boolean
}

interface StrategyHealth {
  status:            'degrading' | 'improving' | 'stable' | 'insufficient_data'
  recent_n:          number
  baseline_n:        number
  recent_win_rate:   number | null
  baseline_win_rate: number | null
  recent_pnl_cents:  number | null
  delta_win_rate:    number | null
}

interface Concentration {
  total_exposure_usd: number
  by_category:        Record<string, { exposure_cents: number; count: number; pct: number }>
  max_category_pct:   number
  max_category_name:  string | null
  largest_position:   { ticker: string | null; exposure_cents: number; side: string | null }
}

interface AdvisorStatus {
  available:          boolean
  last_run_ts?:       string | null
  model_used?:        string
  escalated?:         boolean
  escalation_reasons?: string[]
  strategy_health?:   Record<string, StrategyHealth>
  concentration?:     Concentration
  kelly_by_strategy?: Record<string, { deployed_usd: number; fraction: number; position_count: number }>
  performance_7d?:    { total_pnl_cents: number; win_rate: number; total_trades: number }
  alerts_emitted?:    number
  auto_applied?:      { key: string; value: string } | null
  summary?:           string
  severity_overall?:  string
  run_duration_s?:    number
  message?:           string
}

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

function pnlText(cents: number): string {
  const sign = cents >= 0 ? '+' : ''
  return `${sign}$${(cents / 100).toFixed(2)}`
}

function pct(n: number | null): string {
  if (n == null) return '—'
  return (n * 100).toFixed(1) + '%'
}

// ── Sub-components ────────────────────────────────────────────────────────────

const SEV_STYLES: Record<string, { border: string; badge: string; icon: React.ReactNode }> = {
  critical: {
    border: 'border-l-rose-500',
    badge:  'bg-rose-500/15 text-rose-400 ring-rose-500/30',
    icon:   <AlertTriangle size={13} className="text-rose-400" />,
  },
  warning: {
    border: 'border-l-amber-400',
    badge:  'bg-amber-400/10 text-amber-300 ring-amber-400/25',
    icon:   <AlertTriangle size={13} className="text-amber-400" />,
  },
  info: {
    border: 'border-l-blue-500',
    badge:  'bg-blue-500/10 text-blue-400 ring-blue-500/25',
    icon:   <Info size={13} className="text-blue-400" />,
  },
}

function AlertRow({ a }: { a: AdvisorAlert }) {
  const s   = SEV_STYLES[a.severity] ?? SEV_STYLES.info
  const act = a.action ?? a.recommended_action

  return (
    <div className={`bg-surface-2 rounded-lg border-l-4 ${s.border} px-4 py-3 space-y-1`}>
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-1.5 min-w-0">
          {s.icon}
          <span className="text-slate-100 text-sm font-medium truncate">{a.title}</span>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {a.auto_applied && (
            <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded
                             bg-emerald-500/15 text-emerald-400 ring-1 ring-emerald-500/30">
              auto-applied
            </span>
          )}
          <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded ring-1 uppercase ${s.badge}`}>
            {a.severity}
          </span>
          <span className="text-[10px] text-slate-500">{relativeTime(a.ts)}</span>
        </div>
      </div>
      {a.message && (
        <p className="text-xs text-slate-400 leading-relaxed">{a.message}</p>
      )}
      {act && (
        <p className="text-xs text-slate-500 italic">→ {act}</p>
      )}
    </div>
  )
}

const HEALTH_STYLES = {
  degrading:        { ring: 'ring-rose-500/40',   badge: 'bg-rose-500/15 text-rose-400',   icon: <TrendingDown size={11} /> },
  improving:        { ring: 'ring-emerald-500/40', badge: 'bg-emerald-500/15 text-emerald-400', icon: <TrendingUp size={11} /> },
  stable:           { ring: 'ring-slate-600/40',   badge: 'bg-slate-700/50 text-slate-400',     icon: <Minus size={11} /> },
  insufficient_data:{ ring: 'ring-slate-700/30',   badge: 'bg-slate-800/60 text-slate-500',     icon: <Minus size={11} /> },
}

function StrategyHealthCard({ name, h }: { name: string; h: StrategyHealth }) {
  const sty = HEALTH_STYLES[h.status] ?? HEALTH_STYLES.stable
  const delta = h.delta_win_rate
  const deltaStr = delta == null ? '—' : (delta >= 0 ? '+' : '') + (delta * 100).toFixed(1) + 'pp'
  const deltaColor = delta == null ? 'text-slate-500'
    : delta > 0 ? 'text-emerald-400' : delta < 0 ? 'text-rose-400' : 'text-slate-400'
  const shortName = name.length > 22 ? name.slice(0, 20) + '…' : name

  return (
    <div className={`bg-surface-2 rounded-lg p-3 ring-1 ${sty.ring} space-y-1.5`}>
      <div className="flex items-center justify-between gap-1">
        <span className="text-xs font-medium text-slate-300 truncate" title={name}>{shortName}</span>
        <span className={`flex items-center gap-0.5 text-[10px] font-semibold px-1.5 py-0.5 rounded ${sty.badge}`}>
          {sty.icon}
          {h.status.replace('_', ' ')}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-x-2 text-[10px]">
        <div>
          <span className="text-slate-500">Recent </span>
          <span className="text-slate-300 font-mono">{pct(h.recent_win_rate)}</span>
          <span className="text-slate-600 ml-0.5">({h.recent_n})</span>
        </div>
        <div>
          <span className="text-slate-500">Base </span>
          <span className="text-slate-300 font-mono">{pct(h.baseline_win_rate)}</span>
          <span className="text-slate-600 ml-0.5">({h.baseline_n})</span>
        </div>
      </div>
      <div className="flex items-center justify-between text-[10px]">
        <span className={`font-mono font-semibold ${deltaColor}`}>{deltaStr}</span>
        {h.recent_pnl_cents != null && (
          <span className={h.recent_pnl_cents >= 0 ? 'text-emerald-400 font-mono' : 'text-rose-400 font-mono'}>
            {pnlText(h.recent_pnl_cents)}
          </span>
        )}
      </div>
    </div>
  )
}

function SevBadge({ sev }: { sev: string }) {
  const map: Record<string, string> = {
    critical: 'text-rose-400 bg-rose-500/15 ring-rose-500/30',
    warning:  'text-amber-300 bg-amber-400/10 ring-amber-400/25',
    info:     'text-blue-400 bg-blue-500/10 ring-blue-500/25',
  }
  return (
    <span className={`text-xs font-bold px-2 py-0.5 rounded ring-1 uppercase ${map[sev] ?? map.info}`}>
      {sev}
    </span>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function AdvisorPage() {
  const [sevFilter, setSevFilter] = useState<SeverityFilter>('all')

  const { data: statusData, isFetching: statusLoading, refetch: refetchStatus } = useQuery({
    queryKey:        ['advisor-status'],
    queryFn:         () => advisor.status().then(r => r.data as AdvisorStatus),
    refetchInterval: 30_000,
    staleTime:       30_000,
  })

  const { data: alertsData, isFetching: alertsLoading } = useQuery({
    queryKey:        ['advisor-alerts', sevFilter],
    queryFn:         () => advisor.alerts(30, sevFilter === 'all' ? undefined : sevFilter)
                           .then(r => r.data as { alerts: AdvisorAlert[]; count: number }),
    refetchInterval: 30_000,
    staleTime:       20_000,
  })

  const status  = statusData
  const alerts  = alertsData?.alerts ?? []
  const loading = statusLoading || alertsLoading

  const isEscalated = status?.escalated ?? false
  const modelBadge  = status?.model_used
    ? (status.model_used.includes('sonnet') ? 'sonnet' : 'haiku')
    : null

  return (
    <div className="max-w-6xl mx-auto px-4 py-6 space-y-6">

      {/* ── Header ──────────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-xl flex items-center justify-center shrink-0"
               style={{ background: 'linear-gradient(135deg,#7c3aed,#4f46e5)', boxShadow: '0 0 16px rgba(124,58,237,0.35)' }}>
            <Brain size={18} className="text-white" />
          </div>
          <div>
            <h1 className="text-slate-100 text-xl font-bold tracking-tight">Advisor</h1>
            <p className="text-slate-500 text-xs">Performance monitor · auto-alerts · safe adjustments</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {modelBadge && (
            <span className={`text-xs font-semibold px-2 py-1 rounded ring-1 ${
              isEscalated
                ? 'bg-violet-500/15 text-violet-300 ring-violet-500/30'
                : 'bg-slate-700/60 text-slate-400 ring-slate-600/30'
            }`}>
              {isEscalated && <Zap size={10} className="inline mr-0.5 -mt-0.5" />}
              {modelBadge}
            </span>
          )}
          <button
            onClick={() => refetchStatus()}
            className="p-2 rounded-lg text-slate-500 hover:text-slate-300 hover:bg-surface-2 transition-colors"
            title="Refresh now"
          >
            <RefreshCw size={15} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      {/* ── Last-run summary bar ─────────────────────────────────────────── */}
      {status?.available === false ? (
        <div className="bg-surface-2 rounded-xl p-5 text-center text-slate-500 text-sm">
          {status?.message ?? 'No advisor data yet. Start ep_advisor.py --loop on the exec node.'}
        </div>
      ) : status && (
        <div className="bg-surface-2 rounded-xl p-4 flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-2 text-sm">
            <Clock size={14} className="text-slate-500 shrink-0" />
            <span className="text-slate-400">Last run</span>
            <span className="text-slate-200 font-medium">
              {status.last_run_ts ? relativeTime(status.last_run_ts) : '—'}
            </span>
          </div>
          {status.severity_overall && (
            <SevBadge sev={status.severity_overall} />
          )}
          {status.auto_applied && (
            <div className="flex items-center gap-1.5 text-xs text-emerald-400">
              <CheckCircle size={12} />
              <span>Auto-applied <code className="font-mono text-[11px]">{status.auto_applied.key}={status.auto_applied.value}</code></span>
            </div>
          )}
          {status.performance_7d && (
            <div className="flex items-center gap-3 text-xs ml-auto">
              <span className="text-slate-500">7d P&amp;L</span>
              <span className={`font-mono font-semibold ${(status.performance_7d.total_pnl_cents ?? 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                {pnlText(status.performance_7d.total_pnl_cents ?? 0)}
              </span>
              <span className="text-slate-500">· {status.performance_7d.total_trades ?? 0} trades</span>
            </div>
          )}
          {status.summary && (
            <p className="w-full text-xs text-slate-400 italic border-t border-border pt-2 mt-0.5">
              {status.summary}
            </p>
          )}
        </div>
      )}

      {/* ── Two-column layout ───────────────────────────────────────────── */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">

        {/* ── Left: Alerts ────────────────────────────────────────────── */}
        <div className="lg:col-span-2 space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-slate-200 font-semibold text-sm">Alerts</h2>
            {/* Severity filter pills */}
            <div className="flex gap-1">
              {(['all', 'critical', 'warning', 'info'] as SeverityFilter[]).map(f => (
                <button
                  key={f}
                  onClick={() => setSevFilter(f)}
                  className={`text-[11px] font-medium px-2.5 py-1 rounded-full transition-colors ${
                    sevFilter === f
                      ? 'bg-blue-600 text-white'
                      : 'text-slate-500 hover:text-slate-300 hover:bg-surface-2'
                  }`}
                >
                  {f}
                </button>
              ))}
            </div>
          </div>

          {alerts.length === 0 ? (
            <div className="bg-surface-2 rounded-xl p-8 text-center text-slate-500 text-sm">
              No alerts {sevFilter !== 'all' ? `at ${sevFilter} severity` : ''} yet.
            </div>
          ) : (
            <div className="space-y-2">
              {alerts.map((a, i) => <AlertRow key={a.id ?? i} a={a} />)}
            </div>
          )}
        </div>

        {/* ── Right: Health grid + Concentration ─────────────────────── */}
        <div className="space-y-5">

          {/* Strategy health */}
          <div>
            <h2 className="text-slate-200 font-semibold text-sm mb-3">Strategy Health</h2>
            {!status?.strategy_health || Object.keys(status.strategy_health).length === 0 ? (
              <p className="text-slate-600 text-xs text-center py-4">
                No trade history yet
              </p>
            ) : (
              <div className="space-y-2">
                {Object.entries(status.strategy_health)
                  .sort(([, a], [, b]) => {
                    const order = { degrading: 0, improving: 1, stable: 2, insufficient_data: 3 }
                    return (order[a.status] ?? 4) - (order[b.status] ?? 4)
                  })
                  .map(([name, h]) => (
                    <StrategyHealthCard key={name} name={name} h={h} />
                  ))}
              </div>
            )}
          </div>

          {/* Concentration */}
          {status?.concentration && status.concentration.total_exposure_usd > 0 && (
            <div>
              <h2 className="text-slate-200 font-semibold text-sm mb-3">Concentration</h2>
              <div className="bg-surface-2 rounded-lg p-3 space-y-2">
                <div className="flex justify-between text-xs">
                  <span className="text-slate-500">Total deployed</span>
                  <span className="text-slate-200 font-mono">
                    ${status.concentration.total_exposure_usd.toFixed(2)}
                  </span>
                </div>
                {Object.entries(status.concentration.by_category)
                  .sort(([, a], [, b]) => b.pct - a.pct)
                  .map(([cat, d]) => (
                    <div key={cat}>
                      <div className="flex justify-between text-[11px] mb-0.5">
                        <span className="text-slate-400 capitalize">{cat}</span>
                        <span className={`font-mono ${d.pct > 0.80 ? 'text-rose-400' : d.pct > 0.60 ? 'text-amber-300' : 'text-slate-300'}`}>
                          {(d.pct * 100).toFixed(0)}%
                        </span>
                      </div>
                      <div className="h-1 bg-slate-800 rounded-full overflow-hidden">
                        <div
                          className={`h-full rounded-full transition-all ${
                            d.pct > 0.80 ? 'bg-rose-500' : d.pct > 0.60 ? 'bg-amber-400' : 'bg-blue-500'
                          }`}
                          style={{ width: `${Math.min(100, d.pct * 100)}%` }}
                        />
                      </div>
                    </div>
                  ))}
                {status.concentration.largest_position?.ticker && (
                  <div className="flex justify-between text-[10px] pt-1 border-t border-border">
                    <span className="text-slate-500 truncate">Largest: {status.concentration.largest_position.ticker}</span>
                    <span className="text-slate-400 font-mono shrink-0 ml-1">
                      ${(status.concentration.largest_position.exposure_cents / 100).toFixed(2)}
                    </span>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Kelly by strategy */}
          {status?.kelly_by_strategy && Object.keys(status.kelly_by_strategy).length > 0 && (
            <div>
              <h2 className="text-slate-200 font-semibold text-sm mb-3">Deployed Capital</h2>
              <div className="bg-surface-2 rounded-lg p-3 space-y-1.5">
                {Object.entries(status.kelly_by_strategy)
                  .sort(([, a], [, b]) => b.deployed_usd - a.deployed_usd)
                  .map(([strat, k]) => {
                    const short = strat.length > 20 ? strat.slice(0, 18) + '…' : strat
                    return (
                      <div key={strat} className="flex items-center gap-2 text-[11px]">
                        <span className="text-slate-400 truncate flex-1" title={strat}>{short}</span>
                        <span className="text-slate-300 font-mono shrink-0">
                          ${k.deployed_usd.toFixed(2)}
                        </span>
                        <span className="text-slate-500 font-mono shrink-0 w-10 text-right">
                          {(k.fraction * 100).toFixed(1)}%
                        </span>
                      </div>
                    )
                  })}
              </div>
            </div>
          )}

          {/* Escalation context */}
          {isEscalated && status?.escalation_reasons && status.escalation_reasons.length > 0 && (
            <div className="bg-violet-500/5 rounded-lg p-3 ring-1 ring-violet-500/20">
              <p className="text-violet-400 text-xs font-semibold mb-1.5 flex items-center gap-1">
                <Zap size={11} /> Escalated to Sonnet
              </p>
              <ul className="space-y-0.5">
                {status.escalation_reasons.map((r, i) => (
                  <li key={i} className="text-[10px] text-violet-300/70 font-mono">{r}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
