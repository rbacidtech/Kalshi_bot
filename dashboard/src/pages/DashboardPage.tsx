import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Cell,
  Area, AreaChart, ReferenceLine,
} from 'recharts'
import {
  TrendingUp, TrendingDown, Activity, RefreshCw,
  Bitcoin, DollarSign, Layers, Zap, X, Trash2,
} from 'lucide-react'
import { positions, performance, controls } from '../lib/api'
import { useAuth } from '../lib/auth'
import { useToast } from '../components/Toast'
import axios from 'axios'

// ── Types ─────────────────────────────────────────────────────────────────────

interface Position {
  ticker: string
  side: 'yes' | 'no'
  contracts: number
  entry_cents: number
  fair_value: number | null
  fill_confirmed: boolean
  entered_at: string | null
  close_time: string | null
  unrealized_pnl_cents: number | null
  model_source?: string | null
  confidence?: number | null
  outcome?: string | null
  meeting?: string | null
}

interface PortfolioResponse {
  positions: Position[]
  total_deployed_cents: number
  total_unrealized_pnl_cents: number
  balance_cents: number | null        // Kalshi available cash
  total_value_cents: number | null    // available + deployed
  position_count: number
}

interface PnlPoint {
  ts: string
  balance_cents: number | null
  deployed_cents: number | null
  unrealized_pnl_cents: number | null
  realized_pnl_cents: number | null
  position_count: number | null
}

interface CoinbaseBalance {
  usd_available?: number
  usd_cents?: number
  btc_available?: number
  paper_mode?: boolean
  error?: string
}

interface PerformanceSummary {
  period_days: number
  total_trades: number
  wins: number
  losses: number
  win_rate: number
  total_pnl_cents: number
  avg_pnl_per_trade: number
  avg_hold_time_hours: number
  sharpe_daily: number | null
}

interface ActivityEvent {
  id: string
  event_type: string
  node: string
  detail: string
  ts: string
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtClose(close_time: string | null): string {
  if (!close_time) return '—'
  return new Date(close_time).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

function computeCost(pos: Position): number {
  return pos.side === 'yes'
    ? (pos.entry_cents * pos.contracts) / 100
    : ((100 - pos.entry_cents) * pos.contracts) / 100
}

function fmtPnl(cents: number | null): { text: string; positive: boolean | null } {
  if (cents === null) return { text: '—', positive: null }
  const dollars = cents / 100
  const abs = Math.abs(dollars).toFixed(2)
  return dollars >= 0
    ? { text: `+$${abs}`, positive: true }
    : { text: `-$${abs}`, positive: false }
}

function fmtTimeInTrade(entered_at: string | null): string {
  if (!entered_at) return '—'
  const diffMs = Date.now() - new Date(entered_at).getTime()
  if (diffMs < 0) return '—'
  const totalMins = Math.floor(diffMs / 60_000)
  const days  = Math.floor(totalMins / 1440)
  const hours = Math.floor((totalMins % 1440) / 60)
  const mins  = totalMins % 60
  if (days  > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${mins}m`
  return `${mins}m`
}

function fmtRelTime(ts: string): string {
  const diffMs = Date.now() - new Date(ts).getTime()
  if (diffMs < 0) return 'just now'
  const secs = Math.floor(diffMs / 1000)
  if (secs < 60) return `${secs}s ago`
  const mins = Math.floor(secs / 60)
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  return `${hrs}h ago`
}

function activityDotColor(event_type: string): string {
  if (event_type === 'INTEL_START' || event_type === 'EXEC_START') return 'bg-emerald-500'
  if (event_type === 'DRAWDOWN_HALT') return 'bg-rose-500'
  if (event_type === 'WS_RECONNECT') return 'bg-amber-400'
  return 'bg-slate-600'
}

// ── Win Rate Ring ─────────────────────────────────────────────────────────────

function WinRateRing({ rate }: { rate: number }) {
  const pct    = Math.round(rate * 100)
  const color  = pct >= 55 ? '#34d399' : pct >= 45 ? '#fbbf24' : '#f87171'
  const r      = 16
  const circ   = 2 * Math.PI * r
  const offset = circ * (1 - rate)
  return (
    <div className="relative shrink-0 flex items-center justify-center" style={{ width: 44, height: 44 }}>
      <svg width="44" height="44" className="-rotate-90" style={{ overflow: 'visible' }}>
        <circle cx="22" cy="22" r={r} fill="none" stroke="#1a2238" strokeWidth="4" />
        <circle
          cx="22" cy="22" r={r} fill="none"
          stroke={color} strokeWidth="4"
          strokeDasharray={circ}
          strokeDashoffset={offset}
          strokeLinecap="round"
          style={{ transition: 'stroke-dashoffset 0.5s ease' }}
        />
      </svg>
      <span className="absolute text-[10px] font-bold text-white tabular-nums">{pct}%</span>
    </div>
  )
}

// ── Stat Card ─────────────────────────────────────────────────────────────────

interface StatCardProps {
  label: string
  value: React.ReactNode
  icon?: React.ReactNode
  glow?: 'blue' | 'green' | 'red' | 'amber' | 'none'
  subLabel?: React.ReactNode
}

const GLOW: Record<string, string> = {
  blue:  'border-t-2 border-blue-500/60 shadow-[0_4px_20px_rgba(96,165,250,0.12)]',
  green: 'border-t-2 border-emerald-500/60 shadow-[0_4px_20px_rgba(52,211,153,0.14)]',
  red:   'border-t-2 border-rose-500/60 shadow-[0_4px_20px_rgba(248,113,113,0.14)]',
  amber: 'border-t-2 border-amber-500/60 shadow-[0_4px_20px_rgba(251,191,36,0.12)]',
  none:  '',
}

function StatCard({ label, value, icon, glow = 'none', subLabel }: StatCardProps) {
  return (
    <div className={`card-sm flex flex-col gap-1 min-w-0 ring-1 ring-border/40 transition-shadow duration-300 ${GLOW[glow]}`}>
      <span className="text-xs font-medium text-slate-500 uppercase tracking-wide truncate">{label}</span>
      <div className="flex items-center gap-2 mt-0.5">
        {icon && <span className="shrink-0">{icon}</span>}
        <span className="text-2xl font-bold truncate">{value}</span>
      </div>
      {subLabel && <span className="text-[11px] text-slate-600 mt-0.5">{subLabel}</span>}
    </div>
  )
}

// ── Status Dot ────────────────────────────────────────────────────────────────

function LiveDot({ color = 'green' }: { color?: 'green' | 'amber' | 'red' | 'slate' }) {
  const bg: Record<string, string> = {
    green: 'bg-emerald-500',
    amber: 'bg-amber-400',
    red:   'bg-rose-500',
    slate: 'bg-slate-600',
  }
  const ping: Record<string, string> = {
    green: 'bg-emerald-400',
    amber: 'bg-amber-300',
    red:   'bg-rose-400',
    slate: 'bg-slate-500',
  }
  return (
    <span className="relative flex h-2 w-2 shrink-0">
      {color !== 'slate' && (
        <span className={`animate-ping absolute inline-flex h-full w-full rounded-full ${ping[color]} opacity-60`} />
      )}
      <span className={`relative inline-flex rounded-full h-2 w-2 ${bg[color]}`} />
    </span>
  )
}

// ── Skeleton ──────────────────────────────────────────────────────────────────

function TableSkeleton() {
  return (
    <div className="space-y-3 mt-4">
      {[0, 1, 2].map(i => (
        <div key={i} className="flex gap-3 animate-pulse">
          {[48, 12, 16, 14, 14, 16, 20, 14, 16].map((w, j) => (
            <div key={j} className={`h-5 bg-surface-2 rounded w-${w}`} />
          ))}
        </div>
      ))}
    </div>
  )
}

// ── Chart Tooltips ────────────────────────────────────────────────────────────

function BarTip({ active, payload }: { active?: boolean; payload?: Array<{ value: number; payload: { side: string; ticker: string } }> }) {
  if (!active || !payload?.length) return null
  const { value, payload: item } = payload[0]
  return (
    <div className="bg-surface-1 border border-border rounded-lg px-3 py-2 text-xs shadow-lg">
      <p className="font-mono text-slate-200 mb-0.5">{item.ticker}</p>
      <p className="text-slate-400">${value.toFixed(2)} cost</p>
    </div>
  )
}

function SparkTip({ active, payload, label }: { active?: boolean; payload?: Array<{ value: number }>; label?: string }) {
  if (!active || !payload?.length) return null
  const cents = payload[0]?.value ?? 0
  const pos = cents >= 0
  return (
    <div className="bg-surface-1 border border-border rounded-lg px-3 py-2 text-xs shadow-lg">
      <p className="text-slate-500 mb-0.5">{label}</p>
      <p className={`font-semibold ${pos ? 'text-emerald-400' : 'text-rose-400'}`}>
        {pos ? '+' : '-'}${(Math.abs(cents) / 100).toFixed(2)}
      </p>
    </div>
  )
}

// ── Seconds-ago ───────────────────────────────────────────────────────────────

function SecondsAgo({ since }: { since: number }) {
  const [secs, setSecs] = useState(0)
  useEffect(() => {
    setSecs(Math.floor((Date.now() - since) / 1000))
    const id = setInterval(() => setSecs(Math.floor((Date.now() - since) / 1000)), 1000)
    return () => clearInterval(id)
  }, [since])
  if (secs < 5) return <span className="text-[11px] text-slate-500">Updated just now</span>
  return <span className="text-[11px] text-slate-500">Updated {secs}s ago</span>
}

// ── Position Detail Panel ─────────────────────────────────────────────────────

function DetailRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 py-2 border-b border-border/40 last:border-0">
      <span className="text-xs text-slate-500 shrink-0 pt-0.5">{label}</span>
      <span className="text-xs text-slate-200 text-right">{children}</span>
    </div>
  )
}

function PositionDetailPanel({ pos, onClose, onForceClose, isClosing }: {
  pos: Position
  onClose: () => void
  onForceClose: (ticker: string) => void
  isClosing: boolean
}) {
  const pnl = fmtPnl(pos.unrealized_pnl_cents)
  const [confirmClose, setConfirmClose] = useState(false)

  return (
    <>
      <div
        className="fixed inset-0 bg-black/40 z-40"
        onClick={onClose}
      />
      <div className="fixed inset-y-0 right-0 w-full md:w-96 bg-surface-1 border-l border-border z-50 p-6 overflow-y-auto">
        {/* Header */}
        <div className="flex items-center justify-between mb-5">
          <span className="font-mono text-sm font-bold text-slate-100 leading-tight break-all pr-2">
            {pos.ticker}
          </span>
          <button
            onClick={onClose}
            className="btn-ghost p-1.5 rounded-lg shrink-0"
            title="Close"
          >
            <X size={16} />
          </button>
        </div>

        {/* Details */}
        <div className="space-y-0">
          <DetailRow label="Side">
            {pos.side === 'yes'
              ? <span className="badge-blue">YES</span>
              : <span className="badge-muted">NO</span>
            }
          </DetailRow>

          <DetailRow label="Contracts">
            <span className="tabular-nums">{pos.contracts.toLocaleString()}</span>
          </DetailRow>

          <DetailRow label="Entry price">
            <span className="tabular-nums">{pos.entry_cents}¢</span>
          </DetailRow>

          <DetailRow label="Fair value">
            <span className="tabular-nums">{pos.fair_value != null ? `${Math.round(pos.fair_value * 100)}¢` : '—'}</span>
          </DetailRow>

          <DetailRow label="Unrealized P&L">
            {pnl.positive === null
              ? <span className="text-slate-500">—</span>
              : pnl.positive
                ? <span className="text-emerald-400 font-medium tabular-nums">{pnl.text}</span>
                : <span className="text-rose-400 font-medium tabular-nums">{pnl.text}</span>
            }
          </DetailRow>

          <DetailRow label="Edge at entry">
            <span className="tabular-nums">
              {pos.fair_value != null
                ? `${Math.round(Math.abs(pos.fair_value * 100 - pos.entry_cents))}¢`
                : '—'}
            </span>
          </DetailRow>

          <DetailRow label="Model source">
            <span className="font-mono text-slate-300">{pos.model_source ?? '—'}</span>
          </DetailRow>

          <DetailRow label="Confidence">
            {pos.confidence != null
              ? <span className="tabular-nums">{(pos.confidence * 100).toFixed(0)}%</span>
              : <span className="text-slate-500">—</span>
            }
          </DetailRow>

          <DetailRow label="Outcome">
            <span>{pos.outcome ?? '—'}</span>
          </DetailRow>

          <DetailRow label="Meeting">
            <span>{pos.meeting ?? '—'}</span>
          </DetailRow>

          <DetailRow label="Time in trade">
            <span className="tabular-nums">{fmtTimeInTrade(pos.entered_at)}</span>
          </DetailRow>

          <DetailRow label="Close date">
            <span>{fmtClose(pos.close_time)}</span>
          </DetailRow>

          <DetailRow label="Fill status">
            <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-xs font-medium
              ${pos.fill_confirmed
                ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
                : 'bg-amber-500/10 text-amber-400 border border-amber-500/20'
              }`}>
              <LiveDot color={pos.fill_confirmed ? 'green' : 'amber'} />
              {pos.fill_confirmed ? 'Filled' : 'Pending'}
            </span>
          </DetailRow>
        </div>

        {/* Force close */}
        <div className="mt-6 pt-4 border-t border-border/40">
          {!confirmClose ? (
            <button
              onClick={() => setConfirmClose(true)}
              className="w-full flex items-center justify-center gap-2 py-2 px-4 rounded-xl text-sm font-semibold
                         text-rose-300 border border-rose-500/30 bg-rose-500/8 hover:bg-rose-500/15 transition-colors"
            >
              <Trash2 size={14} />
              Remove from Redis
            </button>
          ) : (
            <div className="space-y-2">
              <p className="text-xs text-amber-300 text-center">
                This removes the Redis record only — it does NOT cancel any live Kalshi order.
              </p>
              <div className="flex gap-2">
                <button
                  onClick={() => setConfirmClose(false)}
                  className="flex-1 py-2 rounded-xl text-xs font-semibold text-slate-400 border border-border hover:bg-surface-2 transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={() => onForceClose(pos.ticker)}
                  disabled={isClosing}
                  className="flex-1 py-2 rounded-xl text-xs font-semibold text-white bg-rose-500/80 hover:bg-rose-500 disabled:opacity-50 transition-colors"
                >
                  {isClosing ? 'Removing…' : 'Confirm Remove'}
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </>
  )
}

// ── Filter / Sort types ───────────────────────────────────────────────────────

type PosFilter = 'all' | 'fomc' | 'weather' | 'yes' | 'no'
type PosSort   = 'default' | 'pnl' | 'age'

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function DashboardPage() {
  const queryClient = useQueryClient()
  const { user } = useAuth()
  const { toast } = useToast()
  const [lastRefresh, setLastRefresh] = useState(Date.now())
  const [selectedPos, setSelectedPos] = useState<Position | null>(null)
  const [posFilter, setPosFilter] = useState<PosFilter>('all')
  const [posSort,   setPosSort]   = useState<PosSort>('default')
  const seenEventIds = useRef<Set<string>>(new Set())

  const closeMut = useMutation({
    mutationFn: (ticker: string) => positions.forceClose(ticker),
    onSuccess: (_data, ticker) => {
      queryClient.invalidateQueries({ queryKey: ['portfolio'] })
      toast(`${ticker} removed from Redis`, 'success', 5000)
      setSelectedPos(null)
    },
    onError: () => toast('Failed to remove position', 'error', 5000),
  })

  const { data, isLoading, isError } = useQuery<PortfolioResponse>({
    queryKey: ['portfolio'],
    queryFn: () => positions.portfolio().then(r => r.data),
    refetchInterval: 60_000,
  })

  const { data: cbData } = useQuery<CoinbaseBalance>({
    queryKey: ['coinbase-balance'],
    queryFn: () => positions.coinbaseBalance().then(r => r.data),
    refetchInterval: 60_000,
    enabled: !!user?.is_admin,
  })

  const { data: btcData } = useQuery<{ amount: string }>({
    queryKey: ['btc-price'],
    queryFn: () => axios.get('https://api.coinbase.com/v2/prices/BTC-USD/spot').then(r => r.data.data),
    refetchInterval: 60_000,
    staleTime: 30_000,
  })

  const { data: ethData } = useQuery<{ amount: string }>({
    queryKey: ['eth-price'],
    queryFn: () => axios.get('https://api.coinbase.com/v2/prices/ETH-USD/spot').then(r => r.data.data),
    refetchInterval: 60_000,
    staleTime: 30_000,
  })

  const { data: historyData } = useQuery<PnlPoint[]>({
    queryKey: ['pnl-history'],
    queryFn: () => performance.history(24).then(r => r.data),
    refetchInterval: 120_000,
    staleTime: 60_000,
  })

  const { data: perfData } = useQuery<PerformanceSummary>({
    queryKey: ['performance', 30],
    queryFn: () => performance.summary(30).then(r => r.data),
    refetchInterval: 60_000,
    staleTime: 30_000,
  })

  const { data: activityData } = useQuery<{ events: ActivityEvent[] }>({
    queryKey: ['activity'],
    queryFn: () => controls.activity(20).then(r => r.data),
    refetchInterval: 60_000,
    enabled: !!user?.is_admin,
  })

  const { data: botStatus } = useQuery<{ session_pnl?: number; balance_cents?: number }>({
    queryKey: ['bot-status'],
    queryFn: () => controls.getStatus().then(r => r.data),
    refetchInterval: 60_000,
    enabled: !!user?.is_admin,
  })

  const { data: botConfig } = useQuery<{ daily_drawdown_limit?: number }>({
    queryKey: ['bot-config'],
    queryFn: () => controls.getConfig().then(r => r.data),
    refetchInterval: 30_000,
    enabled: !!user?.is_admin,
  })

  useEffect(() => { if (data) setLastRefresh(Date.now()) }, [data])

  useEffect(() => {
    if (!activityData?.events) return
    for (const ev of activityData.events) {
      if (seenEventIds.current.has(ev.id)) continue
      seenEventIds.current.add(ev.id)
      if (ev.event_type === 'DRAWDOWN_HALT') {
        toast(`Drawdown halt triggered on ${ev.node}`, 'error', 8000)
      } else if (ev.event_type === 'EXEC_STOP') {
        toast(`Exec node stopped: ${ev.detail}`, 'warning', 6000)
      } else if (ev.event_type === 'WS_RECONNECT') {
        toast(`WebSocket reconnected on ${ev.node}`, 'warning', 4000)
      } else if (ev.event_type === 'EXEC_START') {
        toast(`Exec node started (${ev.detail})`, 'success', 4000)
      }
    }
  }, [activityData])

  // ── Derived ────────────────────────────────────────────────────────────────

  const btcPrice = btcData ? `$${Number(btcData.amount).toLocaleString('en-US', { maximumFractionDigits: 0 })}` : '—'
  const ethPrice = ethData ? `$${Number(ethData.amount).toLocaleString('en-US', { maximumFractionDigits: 0 })}` : '—'
  const cbUsd    = cbData?.usd_available != null ? `$${cbData.usd_available.toFixed(2)}` : null
  const cbBtc    = cbData?.btc_available != null && cbData.btc_available > 0
    ? `${cbData.btc_available.toFixed(6)} BTC` : null
  const cbBtcUsd = cbData?.btc_available != null && cbData.btc_available > 0 && btcData
    ? ` ($${(cbData.btc_available * Number(btcData.amount)).toLocaleString('en-US', { maximumFractionDigits: 0 })})` : ''

  const balance     = data?.total_value_cents != null ? `$${(data.total_value_cents / 100).toFixed(2)}` : '—'
  const available   = data?.balance_cents != null ? `$${(data.balance_cents / 100).toFixed(2)}` : null
  const deployed    = data != null ? `$${(data.total_deployed_cents / 100).toFixed(2)}` : '—'
  const pnlCents    = data?.total_unrealized_pnl_cents ?? null
  const pnlPositive = pnlCents != null && pnlCents >= 0
  const pnlDisplay  = pnlCents != null
    ? `${pnlPositive ? '▲' : '▼'} $${Math.abs(pnlCents / 100).toFixed(2)}`
    : '—'

  const chartData = (data?.positions ?? [])
    .map(p => ({ ticker: p.ticker.slice(-10), fullTicker: p.ticker, cost: computeCost(p), side: p.side }))
    .sort((a, b) => b.cost - a.cost)
    .slice(0, 8)

  // P&L sparkline — format timestamps for display
  const sparkData = (historyData ?? []).map(pt => ({
    time: new Date(pt.ts).toLocaleTimeString('en-US', { hour: 'numeric', hour12: true }),
    pnl:  pt.unrealized_pnl_cents ?? 0,
  }))

  const sparkMin = sparkData.length ? Math.min(...sparkData.map(d => d.pnl)) : 0
  const sparkMax = sparkData.length ? Math.max(...sparkData.map(d => d.pnl)) : 0
  const sparkColor = sparkData.length && sparkData[sparkData.length - 1].pnl >= 0 ? '#34d399' : '#f87171'

  const winRateGlow: 'green' | 'red' = perfData && perfData.win_rate >= 0.50 ? 'green' : 'red'

  const activityEvents = activityData?.events?.slice(0, 10) ?? []
  const showActivity = !!user?.is_admin && activityEvents.length > 0

  // ── Drawdown meter ─────────────────────────────────────────────────────────
  const ddLimit      = botConfig?.daily_drawdown_limit ?? 0.10
  const sessionPnl   = botStatus?.session_pnl ?? 0
  const balanceCents = botStatus?.balance_cents ?? data?.balance_cents ?? null
  const ddCents      = Math.abs(Math.min(0, sessionPnl))
  const ddPct        = balanceCents && balanceCents > 0 ? ddCents / balanceCents : 0
  const ddBarPct     = Math.min(100, (ddPct / ddLimit) * 100)
  const ddColor      = ddBarPct >= 80 ? '#f87171' : ddBarPct >= 50 ? '#fbbf24' : '#34d399'
  const showDD       = !!user?.is_admin && balanceCents !== null

  return (
    <div className="space-y-5 animate-fadeIn">

      {/* ── Page header ───────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-white flex items-center gap-2">
            <span>Portfolio</span>
            <LiveDot color="green" />
          </h1>
          <p className="text-xs text-slate-500 mt-0.5">Live positions · Kalshi</p>
        </div>
        <div className="flex items-center gap-2">
          <SecondsAgo since={lastRefresh} />
          <button
            onClick={() => { queryClient.invalidateQueries({ queryKey: ['portfolio'] }); setLastRefresh(Date.now()) }}
            className="btn-ghost p-2 rounded-lg"
            title="Refresh"
          >
            <RefreshCw size={15} className={isLoading ? 'animate-spin text-blue-400' : ''} />
          </button>
        </div>
      </div>

      {/* ── Kalshi stat cards ─────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
        <StatCard
          label="Kalshi Balance"
          value={<span className="text-white">{balance}</span>}
          glow="blue"
          subLabel={available ? `${available} cash` : 'Total value'}
        />
        <StatCard
          label="Deployed"
          value={deployed}
          glow="blue"
          subLabel="Capital at risk"
        />
        <StatCard
          label="Unrealized P&L"
          value={
            <span className={pnlCents == null ? '' : pnlPositive ? 'text-emerald-400' : 'text-rose-400'}>
              {pnlDisplay}
            </span>
          }
          icon={
            pnlCents != null
              ? pnlPositive
                ? <TrendingUp size={18} className="text-emerald-400" />
                : <TrendingDown size={18} className="text-rose-400" />
              : undefined
          }
          glow={pnlCents == null ? 'none' : pnlPositive ? 'green' : 'red'}
          subLabel="Open positions"
        />
        <StatCard
          label="Open Positions"
          value={
            <span className="flex items-center gap-2">
              <Activity size={18} className="text-blue-400 shrink-0" />
              <span className="text-white">{data?.position_count ?? '—'}</span>
            </span>
          }
          glow="blue"
          subLabel="Active contracts"
        />
        <StatCard
          label="Win Rate · 30d"
          value={
            perfData
              ? <WinRateRing rate={perfData.win_rate} />
              : <span className="text-slate-500">—</span>
          }
          glow={perfData ? winRateGlow : 'none'}
          subLabel={perfData ? `${perfData.wins}W / ${perfData.losses}L` : undefined}
        />
      </div>

      {/* ── Drawdown meter (admin) ────────────────────────────────────────── */}
      {showDD && (
        <div className="rounded-xl border border-border bg-surface-1 px-4 py-3 flex items-center gap-4">
          <div className="flex-1 min-w-0">
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-xs font-medium text-slate-500">Daily Drawdown</span>
              <span className="text-xs tabular-nums" style={{ color: ddColor }}>
                {ddBarPct.toFixed(1)}% of {(ddLimit * 100).toFixed(0)}% limit
              </span>
            </div>
            <div className="w-full h-1.5 bg-surface-3 rounded-full overflow-hidden">
              <div
                className="h-full rounded-full transition-all duration-500"
                style={{ width: `${ddBarPct}%`, background: ddColor }}
              />
            </div>
          </div>
          <span className="text-xs tabular-nums text-slate-400 shrink-0">
            −${(ddCents / 100).toFixed(2)} today
          </span>
        </div>
      )}

      {/* ── Coinbase + crypto prices (admin) ──────────────────────────────── */}
      {user?.is_admin && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatCard
            label="Coinbase USD"
            value={<span className="text-white">{cbUsd ?? (cbData?.error ? 'Error' : '—')}</span>}
            icon={<DollarSign size={16} className="text-slate-400" />}
            glow="none"
          />
          <StatCard
            label="Coinbase BTC"
            value={
              cbBtc
                ? <span className="text-amber-400">{cbBtc}<span className="text-xs text-slate-500 font-normal">{cbBtcUsd}</span></span>
                : <span className="text-slate-500">—</span>
            }
            icon={<Bitcoin size={18} className="text-amber-400" />}
            glow="amber"
          />
          <StatCard
            label="BTC / USD"
            value={<span className="text-amber-400">{btcPrice}</span>}
            icon={<Bitcoin size={16} className="text-amber-500/60" />}
            glow="amber"
          />
          <StatCard
            label="ETH / USD"
            value={<span className="text-blue-400">{ethPrice}</span>}
            icon={<Layers size={16} className="text-blue-500/60" />}
            glow="blue"
          />
        </div>
      )}

      <div className="border-t border-border/40" />

      {/* ── P&L Sparkline (24h history) ───────────────────────────────────── */}
      <div
        className="card ring-1 ring-border/40"
        style={{ borderTop: `2px solid ${sparkColor}40`, boxShadow: `0 4px 24px ${sparkColor}18` }}
      >
        <div className="flex items-center justify-between mb-1">
          <div>
            <h2 className="text-base font-semibold text-slate-300 flex items-center gap-2">
              <Zap size={15} className="text-blue-400" />
              Unrealized P&L — 24h
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">Snapshot history · writes every 60s</p>
          </div>
          {sparkData.length > 0 && (
            <span className={`text-sm font-bold tabular-nums ${sparkData[sparkData.length - 1].pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
              {sparkData[sparkData.length - 1].pnl >= 0 ? '+' : '-'}${(Math.abs(sparkData[sparkData.length - 1].pnl) / 100).toFixed(2)}
            </span>
          )}
        </div>

        {sparkData.length === 0 ? (
          <div className="h-[130px] flex items-center justify-center text-slate-600 text-sm flex-col gap-2">
            <Activity size={24} strokeWidth={1.5} className="text-slate-700" />
            <span>History accumulating — check back in a minute</span>
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={130}>
            <AreaChart data={sparkData} margin={{ top: 8, right: 4, left: 0, bottom: 0 }}>
              <defs>
                <linearGradient id="sparkGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%"  stopColor={sparkColor} stopOpacity={0.25} />
                  <stop offset="95%" stopColor={sparkColor} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid vertical={false} stroke="#1e2d4d" />
              <XAxis dataKey="time" tick={{ fill: '#475569', fontSize: 10 }} axisLine={false} tickLine={false} interval="preserveStartEnd" />
              <YAxis hide domain={[Math.min(sparkMin * 1.2, sparkMin - 50), Math.max(sparkMax * 1.2, sparkMax + 50)]} />
              <Tooltip content={<SparkTip />} cursor={{ stroke: sparkColor, strokeWidth: 1, strokeDasharray: '4 2' }} />
              <ReferenceLine y={0} stroke="#334155" strokeDasharray="3 3" />
              <Area type="monotone" dataKey="pnl" stroke={sparkColor} strokeWidth={2} fill="url(#sparkGrad)" dot={false} activeDot={{ r: 4, fill: sparkColor }} />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>

      {/* ── Position Exposure Bar Chart ───────────────────────────────────── */}
      <div className="card border-t-2 border-blue-500/30 ring-1 ring-border/40 shadow-[0_4px_20px_rgba(96,165,250,0.08)]">
        <h2 className="text-base font-semibold text-slate-300 mb-0.5">
          Position Exposure · {deployed} deployed
        </h2>
        <p className="text-xs text-slate-500 mb-4">Cost basis per open position (top 8)</p>

        {chartData.length === 0 ? (
          <div className="h-[170px] flex items-center justify-center text-slate-500 text-sm">No position data</div>
        ) : (
          <>
            <svg width="0" height="0" style={{ position: 'absolute', overflow: 'hidden' }}>
              <defs>
                <linearGradient id="barGradYes" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#60a5fa" stopOpacity={0.95} />
                  <stop offset="100%" stopColor="#3b82f6" stopOpacity={0.5} />
                </linearGradient>
                <linearGradient id="barGradNo" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#22d3ee" stopOpacity={0.95} />
                  <stop offset="100%" stopColor="#06b6d4" stopOpacity={0.5} />
                </linearGradient>
              </defs>
            </svg>
            <ResponsiveContainer width="100%" height={170}>
              <BarChart data={chartData} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                <CartesianGrid vertical={false} stroke="#1e2d4d" />
                <XAxis dataKey="ticker" tick={{ fill: '#64748b', fontSize: 11, fontFamily: 'JetBrains Mono, monospace' }} axisLine={false} tickLine={false} />
                <YAxis tick={{ fill: '#64748b', fontSize: 11 }} axisLine={false} tickLine={false} tickFormatter={v => `$${v}`} width={44} />
                <Tooltip content={<BarTip />} cursor={{ fill: 'rgba(255,255,255,0.03)' }} />
                <Bar dataKey="cost" radius={[4, 4, 0, 0]}>
                  {chartData.map((entry, idx) => (
                    <Cell key={idx} fill={entry.side === 'yes' ? 'url(#barGradYes)' : 'url(#barGradNo)'} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </>
        )}
      </div>

      {/* ── Live Activity Feed (admin only) ──────────────────────────────────── */}
      {showActivity && (
        <div className="card border-t-2 border-slate-500/30 ring-1 ring-border/40">
          <div className="flex items-center gap-2 mb-3">
            <Activity size={15} className="text-slate-400" />
            <h2 className="text-base font-semibold text-slate-300">Live Activity</h2>
          </div>
          <div className="space-y-0 divide-y divide-border/40">
            {activityEvents.map(ev => (
              <div key={ev.id} className="flex items-start gap-3 py-2.5">
                <span className={`mt-1.5 h-2 w-2 rounded-full shrink-0 ${activityDotColor(ev.event_type)}`} />
                <div className="flex-1 min-w-0">
                  <span className="text-[10px] font-semibold uppercase tracking-widest text-slate-400">{ev.event_type}</span>
                  <p className="text-xs text-slate-300 leading-snug mt-0.5 truncate">{ev.detail}</p>
                </div>
                <span className="text-[11px] text-slate-600 shrink-0 tabular-nums pt-0.5">{fmtRelTime(ev.ts)}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── Positions Table ───────────────────────────────────────────────── */}
      <div className="card border-t-2 border-blue-500/30 ring-1 ring-border/40">
        {(() => {
          const filteredPositions = (data?.positions ?? [])
            .filter(pos => {
              if (posFilter === 'fomc')    return pos.ticker.startsWith('KXFED')
              if (posFilter === 'weather') return pos.ticker.startsWith('KXHIGH') || pos.ticker.startsWith('KXLOW') || pos.ticker.startsWith('KXPRE')
              if (posFilter === 'yes')     return pos.side === 'yes'
              if (posFilter === 'no')      return pos.side === 'no'
              return true
            })
            .sort((a, b) => {
              if (posSort === 'pnl') return (b.unrealized_pnl_cents ?? 0) - (a.unrealized_pnl_cents ?? 0)
              if (posSort === 'age') {
                const aAge = a.entered_at ? Date.now() - new Date(a.entered_at).getTime() : 0
                const bAge = b.entered_at ? Date.now() - new Date(b.entered_at).getTime() : 0
                return bAge - aAge
              }
              return 0
            })
          return (
        <>
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2.5">
            <h2 className="text-base font-semibold text-slate-300">Open Positions</h2>
            {data && (
              <span className="badge-blue">
                {posFilter !== 'all' && filteredPositions.length !== data.position_count
                  ? `${filteredPositions.length} / ${data.position_count}`
                  : data.position_count}
              </span>
            )}
          </div>
          <div className="flex items-center gap-3">
            <SecondsAgo since={lastRefresh} />
            <button
              onClick={() => { queryClient.invalidateQueries({ queryKey: ['portfolio'] }); setLastRefresh(Date.now()) }}
              className="btn-ghost p-2 rounded-lg"
              title="Refresh positions"
            >
              <RefreshCw size={15} className={isLoading ? 'animate-spin text-blue-400' : ''} />
            </button>
          </div>
        </div>

        {/* Filter + sort bar */}
        <div className="flex items-center gap-2 mb-3 overflow-x-auto pb-1 -mx-1 px-1">
          <div className="flex gap-1">
            {(['all','fomc','weather','yes','no'] as PosFilter[]).map(f => (
              <button key={f} onClick={() => setPosFilter(f)}
                className={`px-2.5 py-1 rounded-lg text-xs font-medium transition-all ${
                  posFilter === f
                    ? 'bg-blue-500/20 text-blue-300 border border-blue-500/40'
                    : 'text-slate-500 hover:text-slate-300 border border-transparent'
                }`}>
                {f.toUpperCase()}
              </button>
            ))}
          </div>
          <div className="ml-auto flex gap-1">
            {(['default','pnl','age'] as PosSort[]).map(s => (
              <button key={s} onClick={() => setPosSort(posSort === s ? 'default' : s)}
                className={`px-2.5 py-1 rounded-lg text-xs font-medium transition-all ${
                  posSort === s
                    ? 'bg-indigo-500/20 text-indigo-300 border border-indigo-500/40'
                    : 'text-slate-500 hover:text-slate-300 border border-transparent'
                }`}>
                {s === 'default' ? 'Default' : s === 'pnl' ? 'By P&L' : 'By Age'}
              </button>
            ))}
          </div>
        </div>

        {isLoading && <TableSkeleton />}

        {isError && (
          <div className="py-10 text-center text-rose-400 text-sm">
            Failed to load positions. Please refresh.
          </div>
        )}

        {!isLoading && !isError && (
          data && data.positions.length > 0 ? (
            filteredPositions.length > 0 ? (
              <>
                {/* Mobile cards */}
                <div className="md:hidden space-y-2">
                  {filteredPositions.map((pos, idx) => {
                    const cost    = computeCost(pos)
                    const pnl     = fmtPnl(pos.unrealized_pnl_cents)
                    const avgHoldH = perfData?.avg_hold_time_hours ?? null
                    const ageHours = pos.entered_at ? (Date.now() - new Date(pos.entered_at).getTime()) / 3_600_000 : 0
                    const isAging  = ageHours > (avgHoldH != null && avgHoldH > 0 ? 2 * avgHoldH : 48)
                    return (
                      <div
                        key={`${pos.ticker}-${idx}`}
                        onClick={() => setSelectedPos(pos)}
                        className={`rounded-xl border bg-surface-2/60 p-3.5 cursor-pointer active:bg-surface-2 transition-colors ${
                          pos.side === 'yes' ? 'border-l-4 border-l-blue-500/60 border-r border-t border-b border-border' : 'border-l-4 border-l-cyan-500/60 border-r border-t border-b border-border'
                        }`}
                      >
                        {/* Row 1: ticker + side + status */}
                        <div className="flex items-center justify-between gap-2 mb-2">
                          <span className="font-mono text-sm font-semibold text-slate-100 truncate">{pos.ticker}</span>
                          <div className="flex items-center gap-1.5 shrink-0">
                            {pos.side === 'yes' ? <span className="badge-blue">YES</span> : <span className="badge-muted">NO</span>}
                            <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium ${
                              pos.fill_confirmed ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' : 'bg-amber-500/10 text-amber-400 border border-amber-500/20'
                            }`}>
                              {pos.fill_confirmed ? '✓ Filled' : '⏳ Pending'}
                            </span>
                          </div>
                        </div>
                        {/* Row 2: contracts · entry · fair */}
                        <div className="flex items-center gap-3 text-xs text-slate-400 mb-2">
                          <span>{pos.contracts} contracts</span>
                          <span>·</span>
                          <span>{pos.entry_cents}¢ entry</span>
                          {pos.fair_value != null && <><span>·</span><span className="text-slate-300">FV {Math.round(pos.fair_value * 100)}¢</span></>}
                        </div>
                        {/* Row 3: cost · P&L · aging */}
                        <div className="flex items-center justify-between">
                          <span className="text-xs text-slate-500">${cost.toFixed(2)} cost</span>
                          <div className="flex items-center gap-2">
                            {isAging && (
                              <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-amber-500/15 text-amber-400 border border-amber-500/25">Aging</span>
                            )}
                            <span className={`text-sm font-semibold tabular-nums ${
                              pnl.positive === null ? 'text-slate-500' : pnl.positive ? 'text-emerald-400' : 'text-rose-400'
                            }`}>{pnl.text}</span>
                          </div>
                        </div>
                        {/* Row 4: hold time + close */}
                        <div className="flex items-center justify-between mt-1.5 text-[11px] text-slate-600">
                          <span>{fmtTimeInTrade(pos.entered_at)}</span>
                          <span>Closes {fmtClose(pos.close_time)}</span>
                        </div>
                      </div>
                    )
                  })}
                </div>

                {/* Desktop table */}
                <div className="hidden md:block overflow-x-auto -mx-5 px-5">
                  <table className="w-full text-sm min-w-[780px]">
                    <thead>
                      <tr className="border-b border-border text-left">
                        {['Ticker', 'Side', 'Contracts', 'Entry', 'Fair Value', 'Cost', 'Unrealized P&L', 'Time In Trade', 'Close', 'Status'].map(col => (
                          <th key={col} className="pb-2.5 pr-4 text-xs font-medium text-slate-500 whitespace-nowrap last:pr-0">{col}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border/50">
                      {filteredPositions.map((pos, idx) => {
                        const cost = computeCost(pos)
                        const pnl  = fmtPnl(pos.unrealized_pnl_cents)
                        const rowAccent = pos.side === 'yes' ? 'border-l-2 border-l-blue-500/60' : 'border-l-2 border-l-cyan-500/60'
                        const avgHoldH = perfData?.avg_hold_time_hours ?? null
                        const ageHours = pos.entered_at
                          ? (Date.now() - new Date(pos.entered_at).getTime()) / 3_600_000
                          : 0
                        const isAging = ageHours > (avgHoldH != null && avgHoldH > 0 ? 2 * avgHoldH : 48)
                        return (
                          <tr
                            key={`${pos.ticker}-${idx}`}
                            className={`hover:bg-surface-2/40 transition-colors cursor-pointer ${rowAccent}`}
                            onClick={() => setSelectedPos(pos)}
                          >
                            <td className="py-3 pr-4 pl-2">
                              <span className="font-mono text-sm text-slate-100 tracking-tight">{pos.ticker}</span>
                            </td>
                            <td className="py-3 pr-4">
                              {pos.side === 'yes'
                                ? <span className="badge-blue">YES</span>
                                : <span className="badge-muted">NO</span>
                              }
                            </td>
                            <td className="py-3 pr-4 text-slate-300 tabular-nums">{pos.contracts.toLocaleString()}</td>
                            <td className="py-3 pr-4 text-slate-300 tabular-nums">{pos.entry_cents}¢</td>
                            <td className="py-3 pr-4 text-slate-300 tabular-nums">{pos.fair_value != null ? `${Math.round(pos.fair_value * 100)}¢` : '—'}</td>
                            <td className="py-3 pr-4 text-slate-300 tabular-nums">${cost.toFixed(2)}</td>
                            <td className="py-3 pr-4 tabular-nums">
                              {pnl.positive === null
                                ? <span className="text-slate-500">—</span>
                                : pnl.positive
                                  ? <span className="text-emerald-400 font-medium">{pnl.text}</span>
                                  : <span className="text-rose-400 font-medium">{pnl.text}</span>
                              }
                            </td>
                            <td className="py-3 pr-4 text-slate-500 tabular-nums text-xs">
                              <span>{fmtTimeInTrade(pos.entered_at)}</span>
                              {isAging && (
                                <span className="ml-1.5 px-1.5 py-0.5 rounded text-[10px] font-semibold bg-amber-500/15 text-amber-400 border border-amber-500/25">
                                  Aging
                                </span>
                              )}
                            </td>
                            <td className="py-3 pr-4 text-slate-400 whitespace-nowrap">{fmtClose(pos.close_time)}</td>
                            <td className="py-3">
                              <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-xs font-medium
                                ${pos.fill_confirmed
                                  ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
                                  : 'bg-amber-500/10 text-amber-400 border border-amber-500/20'
                                }`}>
                                <LiveDot color={pos.fill_confirmed ? 'green' : 'amber'} />
                                {pos.fill_confirmed ? 'Filled' : 'Pending'}
                              </span>
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              </>
            ) : (
              <div className="py-10 flex flex-col items-center gap-3 text-slate-500">
                <p className="text-sm font-semibold text-slate-400">No positions match filter</p>
                <button onClick={() => setPosFilter('all')} className="text-xs text-blue-400 hover:text-blue-300 transition-colors">
                  Clear filter
                </button>
              </div>
            )
          ) : (
            <div className="py-20 flex flex-col items-center gap-4 text-slate-500">
              <div className="rounded-full p-5 relative">
                <div className="absolute inset-0 rounded-full bg-blue-500/5 animate-pulse2" />
                <span className="text-5xl relative z-10">🚀</span>
              </div>
              <div className="text-center space-y-1">
                <p className="text-base font-semibold text-slate-400">No open positions</p>
                <p className="text-sm text-slate-600">The bot has no active contracts right now.</p>
              </div>
            </div>
          )
        )}
        </>
          )
        })()}
      </div>

      {/* ── Position Detail Drawer ────────────────────────────────────────── */}
      {selectedPos !== null && (
        <PositionDetailPanel
          pos={selectedPos}
          onClose={() => setSelectedPos(null)}
          onForceClose={ticker => closeMut.mutate(ticker)}
          isClosing={closeMut.isPending}
        />
      )}
    </div>
  )
}
