import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Cell,
  Area, AreaChart, ReferenceLine,
} from 'recharts'
import {
  TrendingUp, TrendingDown, Activity, RefreshCw,
  Bitcoin, DollarSign, Layers, Zap,
} from 'lucide-react'
import { positions, performance } from '../lib/api'
import { useAuth } from '../lib/auth'
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
}

interface PortfolioResponse {
  positions: Position[]
  total_deployed_cents: number
  total_unrealized_pnl_cents: number
  balance_cents: number | null
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

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function DashboardPage() {
  const queryClient = useQueryClient()
  const { user } = useAuth()
  const [lastRefresh, setLastRefresh] = useState(Date.now())

  const { data, isLoading, isError } = useQuery<PortfolioResponse>({
    queryKey: ['portfolio'],
    queryFn: () => positions.portfolio().then(r => r.data),
    refetchInterval: 30_000,
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

  useEffect(() => { if (data) setLastRefresh(Date.now()) }, [data])

  // ── Derived ────────────────────────────────────────────────────────────────

  const btcPrice = btcData ? `$${Number(btcData.amount).toLocaleString('en-US', { maximumFractionDigits: 0 })}` : '—'
  const ethPrice = ethData ? `$${Number(ethData.amount).toLocaleString('en-US', { maximumFractionDigits: 0 })}` : '—'
  const cbUsd    = cbData?.usd_available != null ? `$${cbData.usd_available.toFixed(2)}` : null
  const cbBtc    = cbData?.btc_available != null && cbData.btc_available > 0
    ? `${cbData.btc_available.toFixed(6)} BTC` : null
  const cbBtcUsd = cbData?.btc_available != null && cbData.btc_available > 0 && btcData
    ? ` ($${(cbData.btc_available * Number(btcData.amount)).toLocaleString('en-US', { maximumFractionDigits: 0 })})` : ''

  const balance     = data?.balance_cents != null ? `$${(data.balance_cents / 100).toFixed(2)}` : '—'
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
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatCard
          label="Kalshi Balance"
          value={<span className="text-white">{balance}</span>}
          glow="blue"
          subLabel="Available cash"
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
      </div>

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
          <div className="h-[140px] flex items-center justify-center text-slate-600 text-sm flex-col gap-2">
            <Activity size={24} strokeWidth={1.5} className="text-slate-700" />
            <span>History accumulating — check back in a minute</span>
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={140}>
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
          <div className="h-[200px] flex items-center justify-center text-slate-500 text-sm">No position data</div>
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
            <ResponsiveContainer width="100%" height={200}>
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

      {/* ── Positions Table ───────────────────────────────────────────────── */}
      <div className="card border-t-2 border-blue-500/30 ring-1 ring-border/40">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2.5">
            <h2 className="text-base font-semibold text-slate-300">Open Positions</h2>
            {data && <span className="badge-blue">{data.position_count}</span>}
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

        {isLoading && <TableSkeleton />}

        {isError && (
          <div className="py-10 text-center text-rose-400 text-sm">
            Failed to load positions. Please refresh.
          </div>
        )}

        {!isLoading && !isError && (
          data && data.positions.length > 0 ? (
            <div className="overflow-x-auto -mx-5 px-5">
              <table className="w-full text-sm min-w-[780px]">
                <thead>
                  <tr className="border-b border-border text-left">
                    {['Ticker', 'Side', 'Contracts', 'Entry', 'Fair Value', 'Cost', 'Unrealized P&L', 'Time In Trade', 'Close', 'Status'].map(col => (
                      <th key={col} className="pb-2.5 pr-4 text-xs font-medium text-slate-500 whitespace-nowrap last:pr-0">{col}</th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-border/50">
                  {data.positions.map((pos, idx) => {
                    const cost = computeCost(pos)
                    const pnl  = fmtPnl(pos.unrealized_pnl_cents)
                    const rowAccent = pos.side === 'yes' ? 'border-l-2 border-l-blue-500/60' : 'border-l-2 border-l-cyan-500/60'
                    return (
                      <tr key={`${pos.ticker}-${idx}`} className={`hover:bg-surface-2/40 transition-colors ${rowAccent}`}>
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
                        <td className="py-3 pr-4 text-slate-300 tabular-nums">{pos.fair_value != null ? `${pos.fair_value}¢` : '—'}</td>
                        <td className="py-3 pr-4 text-slate-300 tabular-nums">${cost.toFixed(2)}</td>
                        <td className="py-3 pr-4 tabular-nums">
                          {pnl.positive === null
                            ? <span className="text-slate-500">—</span>
                            : pnl.positive
                              ? <span className="text-emerald-400 font-medium">{pnl.text}</span>
                              : <span className="text-rose-400 font-medium">{pnl.text}</span>
                          }
                        </td>
                        <td className="py-3 pr-4 text-slate-500 tabular-nums text-xs">{fmtTimeInTrade(pos.entered_at)}</td>
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
      </div>
    </div>
  )
}
