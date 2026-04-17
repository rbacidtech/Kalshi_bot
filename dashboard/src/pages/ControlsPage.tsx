import { useState, useEffect, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { controls } from '../lib/api'
import { Save, RotateCcw, Activity, Clock, Wifi, CheckCircle2, AlertTriangle } from 'lucide-react'

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
}

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

// ── Toggle Switch ─────────────────────────────────────────────────────────────

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
        'relative inline-flex h-5 w-9 shrink-0 rounded-full border-2 border-transparent transition-colors duration-200',
        'focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 focus:ring-offset-surface-1',
        disabled ? 'cursor-not-allowed opacity-40' : 'cursor-pointer',
        checked ? 'bg-blue-500' : 'bg-slate-600',
      ].join(' ')}
    >
      <span
        className={[
          'pointer-events-none inline-block h-4 w-4 rounded-full bg-white shadow ring-0 transition-transform duration-200',
          checked ? 'translate-x-4' : 'translate-x-0',
        ].join(' ')}
      />
    </button>
  )
}

// ── Status Card ───────────────────────────────────────────────────────────────

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

function healthColor(health: string | undefined, isAlive: boolean): string {
  if (!isAlive) return 'text-slate-500'
  const h = (health ?? '').toLowerCase()
  if (h.includes('error')) return 'text-rose-400'
  if (h.includes('warn'))  return 'text-amber-400'
  if (h === 'ok' || h === 'healthy') return 'text-emerald-400'
  return 'text-slate-400'
}

function StatusCard({ status }: { status: BotStatus | undefined }) {
  const lastSeen  = status?.last_cycle_at ?? status?.last_balance_at
  const isAlive   = !!lastSeen
  const heartbeat = useTickingRelative(lastSeen)

  // Determine overall running state
  const runState: 'running' | 'degraded' | 'stopped' =
    !isAlive                                     ? 'stopped'
    : (status?.health ?? '').toLowerCase().includes('warn') ? 'degraded'
    : 'running'

  const dotClasses =
    runState === 'running'  ? 'bg-emerald-500 animate-pulse' :
    runState === 'degraded' ? 'bg-amber-400 animate-pulse'   :
    'bg-rose-500'

  const stateLabel =
    runState === 'running'  ? 'Running'  :
    runState === 'degraded' ? 'Degraded' :
    'Stopped'

  const stateColor =
    runState === 'running'  ? 'text-emerald-400' :
    runState === 'degraded' ? 'text-amber-400'   :
    'text-rose-400'

  const hColor = healthColor(status?.health, isAlive)

  return (
    <div className="card">
      {/* Header row */}
      <div className="flex items-center justify-between mb-5">
        <h2 className="text-sm font-semibold text-slate-300">Bot Status</h2>
        <div className="flex items-center gap-2">
          <span className={`w-3 h-3 rounded-full ${dotClasses}`} />
          <span className={`text-xs font-semibold ${stateColor}`}>{stateLabel}</span>
        </div>
      </div>

      {/* Primary metrics grid */}
      <div className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4">
        {/* Mode */}
        <div>
          <p className="text-xs text-slate-500 mb-1.5">Mode</p>
          <span className={status?.mode === 'live' ? 'badge-success' : 'badge-muted'}>
            {status?.mode ? status.mode.toUpperCase() : '—'}
          </span>
        </div>

        {/* Health */}
        <div>
          <p className="text-xs text-slate-500 mb-1.5">Health</p>
          <span className={`text-sm font-medium ${hColor}`}>
            {status?.health ? status.health : isAlive ? 'ok' : '—'}
          </span>
        </div>

        {/* Cycles */}
        <div>
          <p className="text-xs text-slate-500 mb-1.5">Cycles</p>
          <div className="flex items-center gap-1.5">
            <Activity size={13} className="text-slate-500 shrink-0" />
            <span className="text-sm text-slate-200 tabular-nums font-medium">
              {status?.cycle_count != null ? status.cycle_count.toLocaleString() : '—'}
            </span>
          </div>
        </div>

        {/* Uptime */}
        <div>
          <p className="text-xs text-slate-500 mb-1.5">Uptime</p>
          <span className="text-sm text-slate-200 tabular-nums font-medium">
            {fmtUptime(status?.uptime_seconds)}
          </span>
        </div>
      </div>

      {/* Heartbeat + badges row */}
      <div className="mt-4 pt-4 border-t border-border flex flex-wrap items-center gap-x-4 gap-y-2">
        <div className="flex items-center gap-1.5 text-xs text-slate-400">
          <Clock size={12} className="text-slate-500 shrink-0" />
          <span>Last heartbeat: </span>
          <span className="text-slate-200 font-medium tabular-nums">{heartbeat}</span>
        </div>

        {status?.node_id && (
          <span className="badge-muted">node: {status.node_id}</span>
        )}

        <div className="flex items-center gap-1.5 text-xs ml-auto">
          <Wifi size={12} className={status?.ws_connected ? 'text-emerald-400' : 'text-slate-600'} />
          <span className={status?.ws_connected ? 'text-emerald-400' : 'text-slate-500'}>
            {status?.ws_connected ? 'WS connected' : 'WS disconnected'}
          </span>
        </div>

        {status?.balance_cents != null && (
          <span className="text-xs text-slate-400">
            Balance:{' '}
            <span className="text-slate-200 font-mono font-medium">
              ${(status.balance_cents / 100).toFixed(2)}
            </span>
          </span>
        )}
      </div>
    </div>
  )
}

// ── Strategy Toggles ──────────────────────────────────────────────────────────

interface StrategyDef {
  key:         keyof BotConfig
  label:       string
  description: string
  dotClass:    string
}

const STRATEGIES: StrategyDef[] = [
  {
    key: 'enable_fomc',
    label: 'FOMC / Fed rate markets',
    description: 'Core strategy — KXFED and related fed funds rate markets.',
    dotClass: 'bg-blue-500',
  },
  {
    key: 'enable_economic',
    label: 'Economic indicators',
    description: 'CPI, jobs, GDP release markets using FRED data.',
    dotClass: 'bg-purple-500',
  },
  {
    key: 'enable_crypto_price',
    label: 'Crypto price markets',
    description: 'KXBTC and KXETH daily price-range markets (log-normal model).',
    dotClass: 'bg-amber-500',
  },
  {
    key: 'enable_gdp',
    label: 'GDP markets',
    description: 'GDP growth rate prediction markets.',
    dotClass: 'bg-emerald-500',
  },
  {
    key: 'enable_weather',
    label: 'Weather markets',
    description: 'Temperature and precipitation range markets.',
    dotClass: 'bg-cyan-500',
  },
  {
    key: 'enable_sports',
    label: 'Sports markets',
    description: 'Game outcome markets (lower edge — disable for risk reduction).',
    dotClass: 'bg-orange-500',
  },
]

function StrategyToggles({ cfg, update, isSaving }: {
  cfg: BotConfig
  update: <K extends keyof BotConfig>(key: K, value: BotConfig[K]) => void
  isSaving: boolean
}) {
  const activeCount = STRATEGIES.filter(s => cfg[s.key] as boolean).length

  return (
    <div className="card space-y-1">
      {/* Section header */}
      <div className="flex items-center justify-between pb-3 border-b border-border mb-2">
        <h2 className="text-sm font-semibold text-slate-300">Active Strategies</h2>
        <span className={[
          'inline-flex items-center px-2 py-0.5 rounded text-xs font-medium',
          activeCount > 0
            ? 'bg-blue-500/10 text-blue-400'
            : 'bg-surface-3 text-muted',
        ].join(' ')}>
          {activeCount} active
        </span>
      </div>

      {STRATEGIES.map(s => (
        <div key={s.key} className="flex items-center justify-between gap-4 py-2.5">
          <div className="flex items-center gap-3 min-w-0">
            <span className={`w-2.5 h-2.5 rounded-full shrink-0 ${s.dotClass}`} />
            <div className="min-w-0">
              <p className="text-sm text-slate-200 font-medium">{s.label}</p>
              <p className="text-xs text-slate-500 mt-0.5">{s.description}</p>
            </div>
          </div>
          <Toggle
            checked={cfg[s.key] as boolean}
            onChange={v => update(s.key, v as BotConfig[typeof s.key])}
            disabled={isSaving}
          />
        </div>
      ))}
    </div>
  )
}

// ── Risk Parameter Field ──────────────────────────────────────────────────────

interface RiskFieldProps {
  label:       string
  description: string
  value:       number
  displayValue: string
  unit:        string
  onChange:    (v: number) => void
  min?:        number
  max?:        number
  step?:       number
  disabled?:   boolean
}

function RiskField({
  label, description, value, displayValue, unit, onChange, min, max, step, disabled,
}: RiskFieldProps) {
  return (
    <div className="bg-surface-2 rounded-lg p-4 flex flex-col gap-3">
      <div>
        <p className="text-sm text-slate-200 font-medium">{label}</p>
        <p className="text-xs text-slate-500 mt-0.5 leading-snug">{description}</p>
      </div>
      <div className="flex items-center gap-2">
        <div className="text-xl font-semibold text-slate-100 tabular-nums min-w-[3rem]">
          {displayValue}
        </div>
        <span className="text-xs text-slate-500">{unit}</span>
        <input
          type="number"
          className="input w-24 text-right tabular-nums text-sm py-1.5 ml-auto"
          value={value}
          min={min}
          max={max}
          step={step ?? 1}
          disabled={disabled}
          onChange={e => {
            const v = parseFloat(e.target.value)
            if (!isNaN(v)) onChange(v)
          }}
        />
      </div>
    </div>
  )
}

// ── Risk Parameters Section ───────────────────────────────────────────────────

function RiskParameters({ cfg, update, isSaving }: {
  cfg: BotConfig
  update: <K extends keyof BotConfig>(key: K, value: BotConfig[K]) => void
  isSaving: boolean
}) {
  return (
    <div className="card">
      <h2 className="text-sm font-semibold text-slate-300 border-b border-border pb-3 mb-4">
        Risk Parameters
      </h2>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <RiskField
          label="Edge threshold"
          description="Min fee-adjusted edge to enter a position."
          value={cfg.edge_threshold}
          displayValue={(cfg.edge_threshold * 100).toFixed(0)}
          unit="¢"
          onChange={v => update('edge_threshold', Math.round(v * 1000) / 1000)}
          min={0.07} max={0.50} step={0.01}
          disabled={isSaving}
        />
        <RiskField
          label="Max contracts"
          description="Position size cap per individual trade."
          value={cfg.max_contracts}
          displayValue={String(cfg.max_contracts)}
          unit="contracts"
          onChange={v => update('max_contracts', Math.floor(v))}
          min={1} max={100} step={1}
          disabled={isSaving}
        />
        <RiskField
          label="Min confidence"
          description="Minimum signal confidence. Below 0.60 = single-source."
          value={cfg.min_confidence}
          displayValue={(cfg.min_confidence * 100).toFixed(0)}
          unit="%"
          onChange={v => update('min_confidence', Math.round(v * 100) / 100)}
          min={0.10} max={1.00} step={0.05}
          disabled={isSaving}
        />
        <RiskField
          label="Kelly fraction"
          description="Fraction of full Kelly sizing. 0.25 = quarter-Kelly."
          value={cfg.kelly_fraction}
          displayValue={`${cfg.kelly_fraction}×`}
          unit="of full Kelly"
          onChange={v => update('kelly_fraction', Math.round(v * 100) / 100)}
          min={0.05} max={1.00} step={0.05}
          disabled={isSaving}
        />
        <RiskField
          label="Max market exposure"
          description="Max % of balance in any single market."
          value={cfg.max_market_exposure}
          displayValue={(cfg.max_market_exposure * 100).toFixed(0)}
          unit="% per market"
          onChange={v => update('max_market_exposure', Math.round(v * 100) / 100)}
          min={0.01} max={0.50} step={0.01}
          disabled={isSaving}
        />
        <RiskField
          label="Daily drawdown limit"
          description="Stop trading if daily loss exceeds this % of balance."
          value={cfg.daily_drawdown_limit}
          displayValue={(cfg.daily_drawdown_limit * 100).toFixed(0)}
          unit="% of balance"
          onChange={v => update('daily_drawdown_limit', Math.round(v * 100) / 100)}
          min={0.01} max={0.50} step={0.01}
          disabled={isSaving}
        />
      </div>
    </div>
  )
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function ControlsPage() {
  const qc = useQueryClient()

  const { data: serverConfig, isLoading } = useQuery<BotConfig>({
    queryKey: ['bot-config'],
    queryFn: () => controls.getConfig().then(r => r.data),
  })

  const { data: statusData } = useQuery<BotStatus>({
    queryKey: ['bot-status'],
    queryFn: () => controls.getStatus().then(r => r.data),
    refetchInterval: 15_000,
  })

  const [cfg, setCfg]           = useState<BotConfig | null>(null)
  const [dirty, setDirty]       = useState(false)
  const [savedToast, setSavedToast] = useState(false)
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    if (serverConfig && !dirty) setCfg(serverConfig)
  }, [serverConfig])

  const saveMut = useMutation({
    mutationFn: () => controls.patchConfig(cfg as unknown as Record<string, unknown>),
    onSuccess: () => {
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
      <div className="space-y-5 max-w-2xl">
        {[0, 1, 2].map(i => (
          <div key={i} className="card animate-pulse h-40 bg-surface-2" />
        ))}
      </div>
    )
  }

  const isSaving     = saveMut.isPending
  const isPaperTrade = cfg.paper_trade

  return (
    <div className="space-y-0 max-w-2xl">

      {/* ── Paper trading banner ───────────────────────────────────────────── */}
      {isPaperTrade ? (
        <div className="mb-5 flex items-center gap-3 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3">
          <AlertTriangle size={16} className="shrink-0 text-amber-400" />
          <p className="text-sm font-medium text-amber-300">
            Paper trading mode active — no real orders will be placed
          </p>
        </div>
      ) : (
        <div className="mb-5 flex items-center gap-2 rounded-xl border border-emerald-500/20 bg-emerald-500/5 px-4 py-2.5">
          <span className="w-2 h-2 rounded-full bg-emerald-500 shrink-0" />
          <p className="text-xs font-medium text-emerald-400">Live trading active</p>
        </div>
      )}

      {/* ── Page header + save/reset bar ──────────────────────────────────── */}
      <div className="flex items-start justify-between mb-5">
        <div>
          <h1 className="text-xl font-semibold text-slate-100">Bot Controls</h1>
          <p className="text-sm text-slate-500 mt-0.5">
            Manage strategy settings and risk parameters
          </p>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          {/* Saved toast */}
          {savedToast && (
            <div className="flex items-center gap-1.5 text-xs text-emerald-400 font-medium">
              <CheckCircle2 size={13} />
              Saved
            </div>
          )}

          <button
            type="button"
            className="btn-ghost text-xs py-1.5"
            onClick={reset}
            disabled={!dirty || isSaving}
          >
            <RotateCcw size={13} />
            Reset
          </button>

          <button
            type="button"
            className="btn-primary text-xs py-1.5"
            onClick={() => saveMut.mutate()}
            disabled={!dirty || isSaving}
          >
            <Save size={13} />
            {isSaving ? 'Saving…' : dirty ? `Save (${countDirtyFields(cfg, serverConfig)})` : 'Saved'}
          </button>
        </div>
      </div>

      {/* ── Bot status ────────────────────────────────────────────────────── */}
      <StatusCard status={statusData} />

      <hr className="border-border my-6" />

      {/* ── Paper trade toggle ────────────────────────────────────────────── */}
      <div className="card">
        <div className="flex items-center justify-between gap-4">
          <div>
            <p className="text-sm font-semibold text-slate-200">Paper Trading</p>
            <p className="text-xs text-slate-500 mt-0.5">
              Simulate orders — no real money. Disable only after validating signals.
            </p>
          </div>
          <div className="flex items-center gap-3">
            <span className={`text-xs font-medium ${isPaperTrade ? 'text-amber-400' : 'text-emerald-400'}`}>
              {isPaperTrade ? 'Paper' : 'Live'}
            </span>
            <Toggle
              checked={cfg.paper_trade}
              onChange={v => update('paper_trade', v)}
              disabled={isSaving}
            />
          </div>
        </div>
      </div>

      <hr className="border-border my-6" />

      {/* ── Strategy toggles ──────────────────────────────────────────────── */}
      <StrategyToggles cfg={cfg} update={update} isSaving={isSaving} />

      <hr className="border-border my-6" />

      {/* ── Risk parameters ───────────────────────────────────────────────── */}
      <RiskParameters cfg={cfg} update={update} isSaving={isSaving} />

      <hr className="border-border my-6" />

      {/* ── Timing ────────────────────────────────────────────────────────── */}
      <div className="card">
        <h2 className="text-sm font-semibold text-slate-300 border-b border-border pb-3 mb-4">Timing</h2>
        <div className="bg-surface-2 rounded-lg p-4 flex flex-col gap-3">
          <div>
            <p className="text-sm text-slate-200 font-medium">Poll interval</p>
            <p className="text-xs text-slate-500 mt-0.5">
              Seconds between scan cycles. 120s is plenty for FOMC markets.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xl font-semibold text-slate-100 tabular-nums">
              {cfg.poll_interval}
            </span>
            <span className="text-xs text-slate-500">seconds</span>
            <input
              type="number"
              className="input w-24 text-right tabular-nums text-sm py-1.5 ml-auto"
              value={cfg.poll_interval}
              min={30} max={3600} step={30}
              disabled={isSaving}
              onChange={e => {
                const v = parseInt(e.target.value, 10)
                if (!isNaN(v)) update('poll_interval', Math.floor(v))
              }}
            />
          </div>
        </div>
      </div>

      {/* ── Error / bottom padding ────────────────────────────────────────── */}
      {saveMut.isError && (
        <p className="mt-4 text-xs text-rose-400">Failed to save. Please try again.</p>
      )}
      <div className="pb-4" />
    </div>
  )
}

// ── Utility: count changed fields ─────────────────────────────────────────────

function countDirtyFields(
  current: BotConfig,
  server:  BotConfig | undefined,
): number {
  if (!server) return 0
  return (Object.keys(current) as (keyof BotConfig)[]).filter(
    k => current[k] !== server[k],
  ).length
}
