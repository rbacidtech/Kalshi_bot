import { useState, useEffect, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { controls, performance as perfApi } from '../lib/api'
import {
  Save, RotateCcw, Activity, Clock, Wifi, CheckCircle2, AlertTriangle,
  SlidersHorizontal, Cpu, Zap, Bot, ChevronRight,
} from 'lucide-react'

// ── Types ─────────────────────────────────────────────────────────────────────

interface BotConfig {
  enable_fomc:          boolean
  enable_weather:       boolean
  enable_economic:      boolean
  enable_sports:        boolean
  enable_crypto_price:  boolean
  enable_gdp:           boolean
  paper_trade:          boolean
  edge_threshold:       number
  max_contracts:        number
  poll_interval:        number
  min_confidence:       number
  kelly_fraction:       number
  max_market_exposure:  number
  daily_drawdown_limit: number
}

interface SourceHealth {
  status:    string
  age_s:     number | null
  failures:  number
  error:     string
}

interface BotStatus {
  mode?:            string
  cycle_count?:     number
  last_cycle_at?:   string | null
  ws_connected?:    boolean
  balance_cents?:   number
  session_pnl?:     number
  last_balance_at?: string | null
  health?:          string
  uptime_seconds?:  number
  node_id?:         string
  sources?:         Record<string, SourceHealth>
}

type Tab = 'status' | 'strategies' | 'risk' | 'advisor'

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtRelative(iso: string | null | undefined): string {
  if (!iso) return 'Never'
  const diff = Math.floor((Date.now() - new Date(iso).getTime()) / 1000)
  if (diff < 60)   return `${diff}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ${diff % 60}s ago`
  return `${Math.floor(diff / 3600)}h ago`
}

function fmtUptime(seconds: number | undefined): string {
  if (seconds == null) return '—'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  if (h > 0) return `${h}h ${m}m`
  return `${m}m`
}

function useTickingRelative(iso: string | null | undefined): string {
  const [label, setLabel] = useState(() => fmtRelative(iso))
  const prevIso = useRef(iso)
  useEffect(() => {
    prevIso.current = iso
    setLabel(fmtRelative(iso))
    if (!iso) return
    const id = setInterval(() => setLabel(fmtRelative(prevIso.current)), 1000)
    return () => clearInterval(id)
  }, [iso])
  return label
}

// ── Toggle ────────────────────────────────────────────────────────────────────

function Toggle({ checked, onChange, disabled }: {
  checked: boolean; onChange: (v: boolean) => void; disabled?: boolean
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={[
        'relative inline-flex h-6 w-11 shrink-0 rounded-full border-2 border-transparent transition-colors duration-200',
        'focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 focus:ring-offset-surface-1',
        disabled ? 'cursor-not-allowed opacity-40' : 'cursor-pointer',
        checked ? 'bg-gradient-to-r from-blue-500 to-indigo-500' : 'bg-slate-700',
      ].join(' ')}
    >
      <span className={[
        'pointer-events-none inline-block h-5 w-5 rounded-full bg-white shadow-md ring-0 transition-transform duration-200',
        checked ? 'translate-x-5' : 'translate-x-0',
      ].join(' ')} />
    </button>
  )
}

// ── Slider ────────────────────────────────────────────────────────────────────

function RiskSlider({
  label, description, value, display, min, max, step, color, onChange, disabled,
}: {
  label: string
  description: string
  value: number
  display: string
  min: number
  max: number
  step: number
  color: string
  onChange: (v: number) => void
  disabled?: boolean
}) {
  const pct = ((value - min) / (max - min)) * 100

  return (
    <div className="rounded-xl border border-border bg-surface-2 p-4 space-y-3">
      <div className="flex items-start justify-between gap-2">
        <div>
          <p className="text-sm font-semibold text-slate-200">{label}</p>
          <p className="text-xs text-slate-500 mt-0.5 leading-snug">{description}</p>
        </div>
        <span className={`text-lg font-bold tabular-nums shrink-0 ${color}`}>{display}</span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        disabled={disabled}
        onChange={e => onChange(parseFloat(e.target.value))}
        className="w-full h-2 rounded-full appearance-none cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
        style={{
          background: `linear-gradient(to right, #3b82f6 0%, #6366f1 ${pct}%, #1e293b ${pct}%, #1e293b 100%)`,
        }}
      />
      <div className="flex justify-between text-[10px] text-slate-600 tabular-nums">
        <span>{min}</span>
        <span>{max}</span>
      </div>
    </div>
  )
}

// ── Tab Bar ───────────────────────────────────────────────────────────────────

const TABS: { id: Tab; label: string; icon: React.ReactNode }[] = [
  { id: 'status',     label: 'Status',     icon: <Activity size={14} />        },
  { id: 'strategies', label: 'Strategies', icon: <Cpu size={14} />             },
  { id: 'risk',       label: 'Risk',       icon: <SlidersHorizontal size={14} /> },
  { id: 'advisor',    label: 'AI Advisor', icon: <Bot size={14} />             },
]

function TabBar({ active, onChange }: { active: Tab; onChange: (t: Tab) => void }) {
  return (
    <div className="flex gap-1 p-1 rounded-xl bg-surface-2 border border-border mb-6">
      {TABS.map(t => (
        <button
          key={t.id}
          onClick={() => onChange(t.id)}
          className={[
            'flex-1 flex items-center justify-center gap-1.5 py-2 px-3 rounded-lg text-xs font-semibold transition-all duration-150',
            active === t.id
              ? 'bg-gradient-to-r from-blue-600/30 to-indigo-600/20 text-white shadow-[inset_0_0_12px_rgba(96,165,250,0.12)] border border-blue-500/30'
              : 'text-slate-500 hover:text-slate-300 hover:bg-surface-3',
          ].join(' ')}
        >
          {t.icon}
          <span className="hidden sm:inline">{t.label}</span>
        </button>
      ))}
    </div>
  )
}

// ── Status Tab ────────────────────────────────────────────────────────────────

function StatusTab({ status }: { status: BotStatus | undefined }) {
  const lastSeen  = status?.last_cycle_at ?? status?.last_balance_at
  const isAlive   = !!lastSeen
  const heartbeat = useTickingRelative(lastSeen)

  const h = (status?.health ?? '').toLowerCase()
  const runState =
    !isAlive                                 ? 'stopped'
    : h === 'critical' || h.includes('error') ? 'degraded'
    : h.includes('warn')                     ? 'degraded'
    : 'running'

  const stateGradient =
    runState === 'running'  ? 'border-emerald-500/40' :
    runState === 'degraded' ? 'border-amber-500/40'   :
                              'border-rose-500/40'

  const stateColor =
    runState === 'running'  ? 'text-emerald-400' :
    runState === 'degraded' ? 'text-amber-400'   :
    'text-rose-400'

  const dotClasses =
    runState === 'running'  ? 'bg-emerald-500' :
    runState === 'degraded' ? 'bg-amber-400'   :
    'bg-rose-500'

  const stateLabel =
    runState === 'running'  ? 'Running'  :
    runState === 'degraded' ? 'Degraded' :
    'Stopped'

  return (
    <div className="space-y-4">
      {/* Main state banner */}
      <div className={`rounded-xl border bg-surface-1 p-5 ${stateGradient}`}>
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-3">
            <span className="relative flex h-3 w-3">
              {runState !== 'stopped' && (
                <span className={`animate-ping absolute inline-flex h-full w-full rounded-full ${dotClasses} opacity-75`} />
              )}
              <span className={`relative inline-flex rounded-full h-3 w-3 ${dotClasses}`} />
            </span>
            <span className={`text-base font-bold ${stateColor}`}>{stateLabel}</span>
          </div>
          {status?.mode && (
            <span className={status.mode === 'live' ? 'badge-success' : 'badge-muted'}>
              {status.mode.toUpperCase()}
            </span>
          )}
        </div>

        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          {[
            { label: 'Cycles',   value: status?.cycle_count?.toLocaleString() ?? '—' },
            { label: 'Uptime',   value: fmtUptime(status?.uptime_seconds)            },
            { label: 'Health',   value: status?.health ?? (isAlive ? 'ok' : '—')     },
            { label: 'Balance',  value: status?.balance_cents != null ? `$${(status.balance_cents/100).toFixed(2)}` : '—' },
          ].map(m => (
            <div key={m.label} className="bg-black/20 rounded-lg p-3">
              <p className="text-[10px] text-slate-500 uppercase tracking-wider mb-1">{m.label}</p>
              <p className="text-sm font-bold text-slate-100 tabular-nums">{m.value}</p>
            </div>
          ))}
        </div>
      </div>

      {/* Footer row */}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 px-1 text-xs text-slate-500">
        <div className="flex items-center gap-1.5">
          <Clock size={12} />
          <span>Last heartbeat: <span className="text-slate-300 font-medium tabular-nums">{heartbeat}</span></span>
        </div>
        <div className="flex items-center gap-1.5">
          <Wifi size={12} className={status?.ws_connected ? 'text-emerald-400' : ''} />
          <span className={status?.ws_connected ? 'text-emerald-400' : ''}>
            {status?.ws_connected ? 'WS connected' : 'WS off'}
          </span>
        </div>
        {status?.node_id && (
          <span className="badge-muted">node: {status.node_id}</span>
        )}
      </div>

      {/* Data sources grid */}
      {status?.sources && Object.keys(status.sources).length > 0 && (
        <div className="rounded-xl border border-border bg-surface-2 p-4">
          <p className="text-xs font-semibold text-slate-400 mb-3">Data Sources</p>
          <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3">
            {Object.entries(status.sources).map(([name, src]) => {
              const ok = src.status === 'ok'
              return (
                <div key={name} className={`flex items-center gap-2 rounded-lg px-2.5 py-1.5 border text-xs ${
                  ok
                    ? 'bg-emerald-500/8 border-emerald-500/20 text-emerald-400'
                    : 'bg-surface-3 border-border text-slate-500'
                }`}>
                  <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${ok ? 'bg-emerald-400' : 'bg-slate-600'}`} />
                  <span className="truncate font-medium">{name.replace(/_/g, ' ')}</span>
                  {src.age_s != null && ok && (
                    <span className="ml-auto tabular-nums text-[10px] opacity-60">{src.age_s.toFixed(0)}s</span>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}

// ── Strategies Tab ────────────────────────────────────────────────────────────

interface StrategyDef {
  key:         keyof BotConfig
  label:       string
  description: string
  gradient:    string
  glow:        string
}

const STRATEGIES: StrategyDef[] = [
  {
    key: 'enable_fomc',
    label: 'FOMC / Fed Rate',
    description: 'Core strategy — KXFED and related fed funds rate markets.',
    gradient: 'from-blue-600/15 to-blue-600/5',
    glow: 'border-blue-500/30',
  },
  {
    key: 'enable_economic',
    label: 'Economic Indicators',
    description: 'CPI, jobs, GDP release markets using FRED data.',
    gradient: 'from-purple-600/15 to-purple-600/5',
    glow: 'border-purple-500/30',
  },
  {
    key: 'enable_crypto_price',
    label: 'Crypto Price',
    description: 'KXBTC and KXETH daily price-range markets (log-normal model).',
    gradient: 'from-amber-600/15 to-amber-600/5',
    glow: 'border-amber-500/30',
  },
  {
    key: 'enable_gdp',
    label: 'GDP Markets',
    description: 'GDP growth rate prediction markets.',
    gradient: 'from-emerald-600/15 to-emerald-600/5',
    glow: 'border-emerald-500/30',
  },
  {
    key: 'enable_weather',
    label: 'Weather Markets',
    description: 'Temperature and precipitation range markets.',
    gradient: 'from-cyan-600/15 to-cyan-600/5',
    glow: 'border-cyan-500/30',
  },
  {
    key: 'enable_sports',
    label: 'Sports Markets',
    description: 'Game outcome markets (lower edge — disable for risk reduction).',
    gradient: 'from-orange-600/15 to-orange-600/5',
    glow: 'border-orange-500/30',
  },
]

function StrategiesTab({ cfg, update, isSaving }: {
  cfg: BotConfig
  update: <K extends keyof BotConfig>(key: K, value: BotConfig[K]) => void
  isSaving: boolean
}) {
  const activeCount = STRATEGIES.filter(s => cfg[s.key] as boolean).length

  return (
    <div className="space-y-4">
      {/* Paper trade banner inside strategies */}
      <div className={`rounded-xl border p-4 flex items-center justify-between gap-4 ${
        cfg.paper_trade
          ? 'bg-amber-500/10 border-amber-500/30'
          : 'bg-emerald-500/8 border-emerald-500/20'
      }`}>
        <div className="flex items-center gap-3">
          {cfg.paper_trade
            ? <AlertTriangle size={16} className="text-amber-400 shrink-0" />
            : <Zap size={16} className="text-emerald-400 shrink-0" />
          }
          <div>
            <p className={`text-sm font-semibold ${cfg.paper_trade ? 'text-amber-300' : 'text-emerald-400'}`}>
              {cfg.paper_trade ? 'Paper Trading' : 'Live Trading'}
            </p>
            <p className="text-xs text-slate-500 mt-0.5">
              {cfg.paper_trade
                ? 'Simulate orders — no real money placed'
                : 'Real orders active — disable after validating signals'}
            </p>
          </div>
        </div>
        <Toggle checked={cfg.paper_trade} onChange={v => update('paper_trade', v)} disabled={isSaving} />
      </div>

      {/* Active count */}
      <div className="flex items-center justify-between px-1">
        <p className="text-xs text-slate-500 font-medium">Strategy Modules</p>
        <span className="badge-blue">{activeCount} / {STRATEGIES.length} active</span>
      </div>

      <div className="flex items-center gap-2 px-1 py-2 rounded-lg bg-amber-500/8 border border-amber-500/20">
        <AlertTriangle size={13} className="text-amber-400 shrink-0" />
        <p className="text-xs text-amber-300">Strategy toggles and paper mode require a service restart to take effect.</p>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {STRATEGIES.map(s => {
          const active = cfg[s.key] as boolean
          return (
            <div
              key={s.key}
              className={`rounded-xl border bg-gradient-to-br p-4 transition-all duration-200 ${
                active ? `${s.gradient} ${s.glow}` : 'from-surface-2 to-surface-2 border-border opacity-60'
              }`}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-semibold text-slate-200">{s.label}</p>
                  <p className="text-xs text-slate-500 mt-1 leading-snug">{s.description}</p>
                </div>
                <Toggle
                  checked={active}
                  onChange={v => update(s.key, v as BotConfig[typeof s.key])}
                  disabled={isSaving}
                />
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ── Risk Tab ──────────────────────────────────────────────────────────────────

function RiskTab({ cfg, update, isSaving }: {
  // edge_threshold, max_contracts, min_confidence → written to ep:config hash, picked up each scan cycle
  // kelly_fraction, max_market_exposure, daily_drawdown_limit, poll_interval → env-var only, require restart
  cfg: BotConfig
  update: <K extends keyof BotConfig>(key: K, value: BotConfig[K]) => void
  isSaving: boolean
}) {
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2 text-xs">
        <div className="flex items-center gap-2 px-3 py-2 rounded-lg border border-emerald-500/20 bg-emerald-500/8">
          <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 shrink-0" />
          <span className="text-emerald-400 font-medium">Live (next cycle):</span>
          <span className="text-slate-400">Edge · Contracts · Confidence</span>
        </div>
        <div className="flex items-center gap-2 px-3 py-2 rounded-lg border border-amber-500/20 bg-amber-500/8">
          <span className="w-1.5 h-1.5 rounded-full bg-amber-400 shrink-0" />
          <span className="text-amber-400 font-medium">Restart required:</span>
          <span className="text-slate-400">Kelly · Exposure · Drawdown · Poll</span>
        </div>
      </div>

      <RiskSlider
        label="Edge Threshold"
        description="Min fee-adjusted edge required to enter a position."
        value={cfg.edge_threshold}
        display={`${(cfg.edge_threshold * 100).toFixed(0)}¢`}
        min={0.05} max={0.50} step={0.01}
        color="text-blue-400"
        onChange={v => update('edge_threshold', Math.round(v * 1000) / 1000)}
        disabled={isSaving}
      />
      <RiskSlider
        label="Min Confidence"
        description="Minimum signal confidence gate. Below 0.60 = single-source."
        value={cfg.min_confidence}
        display={`${(cfg.min_confidence * 100).toFixed(0)}%`}
        min={0.10} max={1.00} step={0.05}
        color="text-indigo-400"
        onChange={v => update('min_confidence', Math.round(v * 100) / 100)}
        disabled={isSaving}
      />
      <RiskSlider
        label="Kelly Fraction"
        description="Fraction of full Kelly sizing. 0.25 = quarter-Kelly (conservative)."
        value={cfg.kelly_fraction}
        display={`${cfg.kelly_fraction}×`}
        min={0.05} max={1.00} step={0.05}
        color="text-purple-400"
        onChange={v => update('kelly_fraction', Math.round(v * 100) / 100)}
        disabled={isSaving}
      />
      <RiskSlider
        label="Max Contracts"
        description="Position size cap per individual trade."
        value={cfg.max_contracts}
        display={`${cfg.max_contracts}`}
        min={1} max={100} step={1}
        color="text-cyan-400"
        onChange={v => update('max_contracts', Math.floor(v))}
        disabled={isSaving}
      />
      <RiskSlider
        label="Max Market Exposure"
        description="Max % of balance deployed in any single market."
        value={cfg.max_market_exposure}
        display={`${(cfg.max_market_exposure * 100).toFixed(0)}%`}
        min={0.01} max={0.50} step={0.01}
        color="text-amber-400"
        onChange={v => update('max_market_exposure', Math.round(v * 100) / 100)}
        disabled={isSaving}
      />
      <RiskSlider
        label="Daily Drawdown Limit"
        description="Halt trading if daily loss exceeds this % of balance."
        value={cfg.daily_drawdown_limit}
        display={`${(cfg.daily_drawdown_limit * 100).toFixed(0)}%`}
        min={0.01} max={0.50} step={0.01}
        color="text-rose-400"
        onChange={v => update('daily_drawdown_limit', Math.round(v * 100) / 100)}
        disabled={isSaving}
      />
      <RiskSlider
        label="Poll Interval"
        description="Seconds between scan cycles. 120s is fine for FOMC markets."
        value={cfg.poll_interval}
        display={`${cfg.poll_interval}s`}
        min={30} max={600} step={30}
        color="text-slate-300"
        onChange={v => update('poll_interval', Math.floor(v))}
        disabled={isSaving}
      />
    </div>
  )
}

// ── AI Advisor Tab ────────────────────────────────────────────────────────────

function AdvisorTab({ cfg }: { cfg: BotConfig }) {
  const [question, setQuestion] = useState('Review my current settings and suggest improvements.')
  const [suggestion, setSuggestion] = useState<string | null>(null)
  const [loading, setLoading]       = useState(false)
  const [error, setError]           = useState<string | null>(null)

  const { data: perf } = useQuery({
    queryKey: ['performance', 30],
    queryFn: () => perfApi.summary(30).then(r => r.data),
  })

  async function ask() {
    setLoading(true)
    setError(null)
    setSuggestion(null)
    try {
      const res = await controls.aiSuggest(
        cfg as unknown as Record<string, unknown>,
        question,
        perf as Record<string, unknown> | undefined,
      )
      if (res.data.ok) {
        setSuggestion(res.data.suggestion)
      } else {
        setError(res.data.suggestion ?? 'Unknown error')
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Request failed')
    } finally {
      setLoading(false)
    }
  }

  const PRESETS = [
    'Review my current settings and suggest improvements.',
    'Am I being too aggressive? How should I reduce risk?',
    'How can I increase win rate without hurting P&L?',
    'Which strategies should I disable given low volume?',
  ]

  return (
    <div className="space-y-4">
      {/* Header card */}
      <div className="rounded-xl border bg-surface-1 p-4" style={{ borderTopColor: '#6366f1', borderTopWidth: 3, boxShadow: '0 4px 24px rgba(99,102,241,0.12)' }}>
        <div className="flex items-center gap-3 mb-1">
          <div
            className="w-8 h-8 rounded-lg flex items-center justify-center text-base shrink-0"
            style={{ background: 'linear-gradient(135deg, #6366f1 0%, #3b82f6 100%)', boxShadow: '0 0 12px rgba(99,102,241,0.4)' }}
          >
            🤖
          </div>
          <div>
            <p className="text-sm font-bold text-slate-200">Claude Advisor</p>
            <p className="text-xs text-slate-500">Algorithmic trading parameter expert</p>
          </div>
        </div>
      </div>

      {/* Preset questions */}
      <div className="flex flex-wrap gap-2">
        {PRESETS.map(p => (
          <button
            key={p}
            onClick={() => setQuestion(p)}
            className={`text-xs px-3 py-1.5 rounded-lg border transition-all duration-150 ${
              question === p
                ? 'bg-indigo-500/20 border-indigo-500/40 text-indigo-300'
                : 'bg-surface-2 border-border text-slate-400 hover:text-slate-200 hover:border-slate-600'
            }`}
          >
            {p.length > 48 ? p.slice(0, 48) + '…' : p}
          </button>
        ))}
      </div>

      {/* Textarea */}
      <textarea
        rows={3}
        value={question}
        onChange={e => setQuestion(e.target.value)}
        placeholder="Ask about your trading parameters…"
        className="w-full bg-surface-2 border border-border rounded-xl px-4 py-3 text-sm text-slate-200
                   placeholder-slate-600 resize-none focus:outline-none focus:ring-2 focus:ring-indigo-500/50
                   focus:border-indigo-500/50 transition-colors"
      />

      <button
        type="button"
        onClick={ask}
        disabled={loading || !question.trim()}
        className="w-full flex items-center justify-center gap-2 py-2.5 px-4 rounded-xl text-sm font-semibold
                   text-white disabled:opacity-50 disabled:cursor-not-allowed transition-all duration-150"
        style={{ background: 'linear-gradient(135deg, #6366f1 0%, #3b82f6 100%)', boxShadow: loading ? 'none' : '0 0 16px rgba(99,102,241,0.3)' }}
      >
        {loading ? (
          <>
            <span className="animate-spin inline-block w-4 h-4 border-2 border-white/30 border-t-white rounded-full" />
            Asking Claude…
          </>
        ) : (
          <>
            <Bot size={15} />
            Get Suggestions
            <ChevronRight size={14} />
          </>
        )}
      </button>

      {/* Response */}
      {error && (
        <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 p-4">
          <p className="text-sm text-rose-300">{error}</p>
        </div>
      )}

      {suggestion && (
        <div
          className="rounded-xl border border-border bg-surface-1 p-5 space-y-2"
          style={{ borderTopColor: '#6366f1', borderTopWidth: 3 }}
        >
          <div className="flex items-center gap-2 mb-3">
            <div className="w-5 h-5 rounded-md flex items-center justify-center text-xs shrink-0"
              style={{ background: 'linear-gradient(135deg, #6366f1, #3b82f6)' }}>
              🤖
            </div>
            <p className="text-xs font-semibold text-indigo-400">Claude's Suggestion</p>
          </div>
          <div className="text-sm text-slate-300 leading-relaxed whitespace-pre-wrap">
            {suggestion}
          </div>
        </div>
      )}
    </div>
  )
}

// ── Main Page ─────────────────────────────────────────────────────────────────

function countDirtyFields(current: BotConfig, server: BotConfig | undefined): number {
  if (!server) return 0
  return (Object.keys(current) as (keyof BotConfig)[]).filter(k => current[k] !== server[k]).length
}

export default function ControlsPage() {
  const qc = useQueryClient()
  const [activeTab, setActiveTab] = useState<Tab>('status')

  const { data: serverConfig, isLoading } = useQuery<BotConfig>({
    queryKey: ['bot-config'],
    queryFn:  () => controls.getConfig().then(r => r.data),
  })

  const { data: statusData } = useQuery<BotStatus>({
    queryKey:       ['bot-status'],
    queryFn:        () => controls.getStatus().then(r => r.data),
    refetchInterval: 15_000,
  })

  const [cfg, setCfg]               = useState<BotConfig | null>(null)
  const [dirty, setDirty]           = useState(false)
  const [savedToast, setSavedToast] = useState(false)
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    if (serverConfig && !dirty) setCfg(serverConfig)
  }, [serverConfig])

  const saveMut = useMutation({
    mutationFn: () => controls.patchConfig(cfg as unknown as Record<string, unknown>),
    onSuccess:  () => {
      qc.invalidateQueries({ queryKey: ['bot-config'] })
      setDirty(false)
      setSavedToast(true)
      if (toastTimer.current) clearTimeout(toastTimer.current)
      toastTimer.current = setTimeout(() => setSavedToast(false), 3000)
    },
  })

  function update<K extends keyof BotConfig>(key: K, value: BotConfig[K]) {
    setCfg(prev => prev ? { ...prev, [key]: value } : prev)
    setDirty(true)
  }

  function reset() {
    if (serverConfig) { setCfg(serverConfig); setDirty(false) }
  }

  if (isLoading || !cfg) {
    return (
      <div className="space-y-4 max-w-2xl">
        <div className="h-12 rounded-xl bg-surface-2 animate-pulse" />
        {[0, 1, 2].map(i => (
          <div key={i} className="h-36 rounded-xl bg-surface-2 animate-pulse" />
        ))}
      </div>
    )
  }

  const isSaving   = saveMut.isPending
  const dirtyCount = countDirtyFields(cfg, serverConfig)
  const showSave   = activeTab === 'strategies' || activeTab === 'risk'

  return (
    <div className="max-w-2xl space-y-0">

      {/* ── Page header ──────────────────────────────────────────────────── */}
      <div className="flex items-start justify-between mb-5">
        <div>
          <h1 className="text-xl font-bold text-slate-100 flex items-center gap-2">
            <SlidersHorizontal size={18} className="text-indigo-400" />
            Bot Controls
          </h1>
          <p className="text-sm text-slate-500 mt-0.5">Manage strategy settings and risk parameters</p>
        </div>

        {/* Save / Reset — only shown on editable tabs */}
        {showSave && (
          <div className="flex items-center gap-2 shrink-0">
            {savedToast && (
              <span className="flex items-center gap-1.5 text-xs text-emerald-400 font-medium">
                <CheckCircle2 size={13} /> Saved
              </span>
            )}
            <button
              type="button"
              className="btn-ghost text-xs py-1.5"
              onClick={reset}
              disabled={!dirty || isSaving}
            >
              <RotateCcw size={13} /> Reset
            </button>
            <button
              type="button"
              className="btn-primary text-xs py-1.5"
              onClick={() => saveMut.mutate()}
              disabled={!dirty || isSaving}
            >
              <Save size={13} />
              {isSaving ? 'Saving…' : dirty ? `Save (${dirtyCount})` : 'Saved'}
            </button>
          </div>
        )}
      </div>

      {/* ── Tab bar ──────────────────────────────────────────────────────── */}
      <TabBar active={activeTab} onChange={setActiveTab} />

      {/* ── Tab content ──────────────────────────────────────────────────── */}
      {activeTab === 'status'     && <StatusTab     status={statusData} />}
      {activeTab === 'strategies' && <StrategiesTab cfg={cfg} update={update} isSaving={isSaving} />}
      {activeTab === 'risk'       && <RiskTab       cfg={cfg} update={update} isSaving={isSaving} />}
      {activeTab === 'advisor'    && <AdvisorTab    cfg={cfg} />}

      {saveMut.isError && (
        <p className="mt-4 text-xs text-rose-400">Failed to save. Please try again.</p>
      )}
      <div className="pb-6" />
    </div>
  )
}
