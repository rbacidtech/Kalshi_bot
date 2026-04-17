import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Cell,
} from 'recharts'
import { TrendingUp, TrendingDown, Activity, RefreshCw, Inbox, Bitcoin, DollarSign, Layers } from 'lucide-react'
import { positions } from '../lib/api'
import { useAuth } from '../lib/auth'
import axios from 'axios'

// ── Types ────────────────────────────────────────────────────────────────────

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

// ── Helpers ──────────────────────────────────────────────────────────────────

function formatCloseTime(close_time: string | null): string {
  if (!close_time) return '—'
  return new Date(close_time).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

function computeCost(position: Position): number {
  if (position.side === 'yes') {
    return (position.entry_cents * position.contracts) / 100
  }
  return ((100 - position.entry_cents) * position.contracts) / 100
}

function formatPnl(cents: number | null): { text: string; positive: boolean | null } {
  if (cents === null) return { text: '—', positive: null }
  const dollars = cents / 100
  const abs = Math.abs(dollars).toFixed(2)
  if (dollars >= 0) return { text: `+$${abs}`, positive: true }
  return { text: `-$${abs}`, positive: false }
}

/** Format duration between a past ISO timestamp and now as "3h 12m" or "2d 4h" */
function formatTimeInTrade(entered_at: string | null): string {
  if (!entered_at) return '—'
  const diffMs = Date.now() - new Date(entered_at).getTime()
  if (diffMs < 0) return '—'
  const totalMins = Math.floor(diffMs / 60_000)
  const days = Math.floor(totalMins / 1440)
  const hours = Math.floor((totalMins % 1440) / 60)
  const mins = totalMins % 60
  if (days > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${mins}m`
  return `${mins}m`
}

// ── Stat Card ────────────────────────────────────────────────────────────────

interface StatCardProps {
  label: string
  value: React.ReactNode
  icon?: React.ReactNode
  accentClass?: string   // e.g. "border-blue-500/40"
  subLabel?: React.ReactNode
}

function StatCard({ label, value, icon, accentClass, subLabel }: StatCardProps) {
  return (
    <div
      className={[
        'card-sm flex flex-col gap-1 min-w-0 ring-1 ring-border/40',
        accentClass ? `border-t-2 ${accentClass}` : '',
      ].join(' ')}
    >
      <span className="text-xs font-medium text-slate-500 uppercase tracking-wide truncate">{label}</span>
      <div className="flex items-center gap-2 mt-0.5">
        {icon && <span className="shrink-0">{icon}</span>}
        <span className="text-2xl font-bold truncate">{value}</span>
      </div>
      {subLabel && (
        <span className="text-[11px] text-slate-600 mt-0.5">{subLabel}</span>
      )}
    </div>
  )
}

// ── Loading Skeleton ─────────────────────────────────────────────────────────

function TableSkeleton() {
  return (
    <div className="space-y-3 mt-4">
      {[0, 1, 2].map(i => (
        <div key={i} className="flex gap-3 animate-pulse">
          <div className="h-5 bg-surface-2 rounded w-48" />
          <div className="h-5 bg-surface-2 rounded w-12" />
          <div className="h-5 bg-surface-2 rounded w-16" />
          <div className="h-5 bg-surface-2 rounded w-14" />
          <div className="h-5 bg-surface-2 rounded w-14" />
          <div className="h-5 bg-surface-2 rounded w-16" />
          <div className="h-5 bg-surface-2 rounded w-20" />
          <div className="h-5 bg-surface-2 rounded w-14" />
          <div className="h-5 bg-surface-2 rounded w-16" />
        </div>
      ))}
    </div>
  )
}

// ── Chart Tooltip ────────────────────────────────────────────────────────────

function ChartTooltip({ active, payload }: { active?: boolean; payload?: Array<{ value: number; payload: { side: string; ticker: string } }> }) {
  if (!active || !payload?.length) return null
  const { value, payload: item } = payload[0]
  return (
    <div className="bg-surface-1 border border-border rounded-lg px-3 py-2 text-xs shadow-lg">
      <p className="font-mono text-slate-200 mb-0.5">{item.ticker}</p>
      <p className="text-slate-400">${value.toFixed(2)} cost</p>
    </div>
  )
}

// ── Seconds-ago counter ──────────────────────────────────────────────────────

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

// ── Main Page ────────────────────────────────────────────────────────────────

interface CoinbaseBalance {
  usd_available?: number
  usd_cents?: number
  btc_available?: number
  paper_mode?: boolean
  error?: string
}

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
    queryFn: () =>
      axios.get('https://api.coinbase.com/v2/prices/BTC-USD/spot').then(r => r.data.data),
    refetchInterval: 60_000,
    staleTime: 30_000,
  })

  const { data: ethData } = useQuery<{ amount: string }>({
    queryKey: ['eth-price'],
    queryFn: () =>
      axios.get('https://api.coinbase.com/v2/prices/ETH-USD/spot').then(r => r.data.data),
    refetchInterval: 60_000,
    staleTime: 30_000,
  })

  // Update lastRefresh whenever data arrives
  useEffect(() => {
    if (data) setLastRefresh(Date.now())
  }, [data])

  // ── Derived values ──────────────────────────────────────────────────────

  const btcPrice = btcData ? `$${Number(btcData.amount).toLocaleString('en-US', { maximumFractionDigits: 0 })}` : '—'
  const ethPrice = ethData ? `$${Number(ethData.amount).toLocaleString('en-US', { maximumFractionDigits: 0 })}` : '—'

  // Coinbase balance
  const cbUsd = cbData?.usd_available != null ? `$${cbData.usd_available.toFixed(2)}` : null
  const cbBtc = cbData?.btc_available != null && cbData.btc_available > 0
    ? `${cbData.btc_available.toFixed(6)} BTC`
    : null
  const cbBtcUsd = cbData?.btc_available != null && cbData.btc_available > 0 && btcData
    ? ` ($${(cbData.btc_available * Number(btcData.amount)).toLocaleString('en-US', { maximumFractionDigits: 0 })})`
    : ''

  const balance = data?.balance_cents != null ? `$${(data.balance_cents / 100).toFixed(2)}` : '—'
  const deployed = data != null ? `$${(data.total_deployed_cents / 100).toFixed(2)}` : '—'
  const pnlCents = data?.total_unrealized_pnl_cents ?? null
  const pnlPositive = pnlCents != null && pnlCents >= 0
  const pnlArrow = pnlCents != null ? (pnlPositive ? '▲' : '▼') : ''
  const pnlDisplay =
    pnlCents != null
      ? `${pnlArrow} $${Math.abs(pnlCents / 100).toFixed(2)}`
      : '—'

  // ── Chart data ──────────────────────────────────────────────────────────

  const chartData = (data?.positions ?? [])
    .map(p => ({
      ticker: p.ticker.slice(-10),
      fullTicker: p.ticker,
      cost: computeCost(p),
      side: p.side,
    }))
    .sort((a, b) => b.cost - a.cost)
    .slice(0, 8)

  const deployedSubtitle = data != null
    ? `Position Exposure · ${deployed} deployed`
    : 'Position Exposure'

  return (
    <div className="space-y-5">

      {/* ── 1. Stats Row — Kalshi ────────────────────────────────────────── */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatCard
          label="Kalshi Balance"
          value={<span className="text-white">{balance}</span>}
          accentClass="border-blue-500/40"
          subLabel="Available cash"
        />
        <StatCard
          label="Deployed"
          value={deployed}
          accentClass="border-purple-500/40"
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
          accentClass={pnlCents == null ? 'border-slate-500/30' : pnlPositive ? 'border-emerald-500/40' : 'border-rose-500/40'}
          subLabel="30-day window"
        />
        <StatCard
          label="Open Positions"
          value={
            <span className="flex items-center gap-2">
              <Activity size={18} className="text-slate-400 shrink-0" />
              {data?.position_count ?? '—'}
            </span>
          }
          accentClass="border-slate-500/30"
          subLabel="Active contracts"
        />
      </div>

      {/* ── 1b. Coinbase Balance + Crypto Prices ──────────────────────────── */}
      {user?.is_admin && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatCard
            label="Coinbase USD"
            value={<span className="text-white">{cbUsd ?? (cbData?.error ? 'Error' : '—')}</span>}
            icon={<DollarSign size={16} className="text-slate-400" />}
            accentClass="border-slate-500/30"
          />
          <StatCard
            label="Coinbase BTC"
            value={
              cbBtc
                ? <span className="text-amber-400">{cbBtc}<span className="text-xs text-slate-500 font-normal">{cbBtcUsd}</span></span>
                : <span className="text-slate-500">—</span>
            }
            icon={<Bitcoin size={18} className="text-amber-400" />}
            accentClass="border-amber-500/30"
          />
          <StatCard
            label="BTC / USD"
            value={<span className="text-amber-400">{btcPrice}</span>}
            icon={<Bitcoin size={16} className="text-amber-500/60" />}
            accentClass="border-amber-500/30"
          />
          <StatCard
            label="ETH / USD"
            value={<span className="text-blue-400">{ethPrice}</span>}
            icon={<Layers size={16} className="text-blue-500/60" />}
            accentClass="border-blue-500/30"
          />
        </div>
      )}

      {/* ── Divider ──────────────────────────────────────────────────────── */}
      <div className="border-t border-border/40" />

      {/* ── 2. Mini Bar Chart ────────────────────────────────────────────── */}
      <div className="card border-t-2 border-blue-500/30 ring-1 ring-border/40">
        <h2 className="text-base font-semibold text-slate-300 mb-0.5">{deployedSubtitle}</h2>
        <p className="text-xs text-slate-500 mb-4">Cost basis per open position (top 8)</p>
        {chartData.length === 0 ? (
          <div className="h-[220px] flex items-center justify-center text-slate-500 text-sm">
            No position data
          </div>
        ) : (
          <>
            {/* Hidden SVG to declare gradient defs shared with the Recharts canvas */}
            <svg width="0" height="0" style={{ position: 'absolute', overflow: 'hidden' }}>
              <defs>
                <linearGradient id="barGradYes" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#60a5fa" stopOpacity={0.95} />
                  <stop offset="100%" stopColor="#3b82f6" stopOpacity={0.6} />
                </linearGradient>
                <linearGradient id="barGradNo" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#22d3ee" stopOpacity={0.95} />
                  <stop offset="100%" stopColor="#06b6d4" stopOpacity={0.6} />
                </linearGradient>
              </defs>
            </svg>
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={chartData} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid vertical={false} stroke="#1e2d4d" />
              <XAxis
                dataKey="ticker"
                tick={{ fill: '#64748b', fontSize: 11, fontFamily: 'JetBrains Mono, monospace' }}
                axisLine={false}
                tickLine={false}
              />
              <YAxis
                tick={{ fill: '#64748b', fontSize: 11 }}
                axisLine={false}
                tickLine={false}
                tickFormatter={v => `$${v}`}
                width={44}
              />
              <Tooltip content={<ChartTooltip />} cursor={{ fill: 'rgba(255,255,255,0.04)' }} />
              <Bar dataKey="cost" radius={[4, 4, 0, 0]}>
                {chartData.map((entry, idx) => (
                  <Cell
                    key={idx}
                    fill={entry.side === 'yes' ? 'url(#barGradYes)' : 'url(#barGradNo)'}
                  />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
          </>
        )}
      </div>

      {/* ── 3 + 4. Positions Table ───────────────────────────────────────── */}
      <div className="card border-t-2 border-blue-500/30 ring-1 ring-border/40">
        {/* Table header row */}
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2.5">
            <h2 className="text-base font-semibold text-slate-300">Open Positions</h2>
            {data && (
              <span className="badge-blue">{data.position_count}</span>
            )}
          </div>

          {/* Refresh button + timestamp */}
          <div className="flex items-center gap-3">
            <SecondsAgo since={lastRefresh} />
            <button
              onClick={() => {
                queryClient.invalidateQueries({ queryKey: ['portfolio'] })
                setLastRefresh(Date.now())
              }}
              className="btn-ghost p-2 rounded-lg"
              title="Refresh positions"
              aria-label="Refresh positions"
            >
              <RefreshCw size={15} className={isLoading ? 'animate-spin' : ''} />
            </button>
          </div>
        </div>

        {/* Loading */}
        {isLoading && <TableSkeleton />}

        {/* Error */}
        {isError && (
          <div className="py-10 text-center text-rose-400 text-sm">
            Failed to load positions. Please refresh.
          </div>
        )}

        {/* Table */}
        {!isLoading && !isError && (
          <>
            {data && data.positions.length > 0 ? (
              <div className="overflow-x-auto -mx-5 px-5">
                <table className="w-full text-sm min-w-[780px]">
                  <thead>
                    <tr className="border-b border-border text-left">
                      {['Ticker', 'Side', 'Contracts', 'Entry', 'Fair Value', 'Cost', 'Unrealized P&L', 'Time In Trade', 'Close', 'Status'].map(col => (
                        <th key={col} className="pb-2.5 pr-4 text-xs font-medium text-slate-500 whitespace-nowrap last:pr-0">
                          {col}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border/50">
                    {data.positions.map((pos, idx) => {
                      const cost = computeCost(pos)
                      const pnl = formatPnl(pos.unrealized_pnl_cents)
                      const timeInTrade = formatTimeInTrade(pos.entered_at)
                      const rowAccent = pos.side === 'yes'
                        ? 'border-l-2 border-l-blue-500/50'
                        : 'border-l-2 border-l-cyan-500/50'
                      return (
                        <tr
                          key={`${pos.ticker}-${idx}`}
                          className={`hover:bg-surface-2/40 transition-colors ${rowAccent}`}
                        >
                          {/* Ticker */}
                          <td className="py-3 pr-4 pl-2">
                            <span className="font-mono text-sm text-slate-100 tracking-tight cursor-default hover:text-white transition-colors">
                              {pos.ticker}
                            </span>
                          </td>

                          {/* Side */}
                          <td className="py-3 pr-4">
                            {pos.side === 'yes'
                              ? <span className="badge-blue">YES</span>
                              : <span className="badge-muted">NO</span>
                            }
                          </td>

                          {/* Contracts */}
                          <td className="py-3 pr-4 text-slate-300 tabular-nums">
                            {pos.contracts.toLocaleString()}
                          </td>

                          {/* Entry */}
                          <td className="py-3 pr-4 text-slate-300 tabular-nums">
                            {pos.entry_cents}¢
                          </td>

                          {/* Fair Value */}
                          <td className="py-3 pr-4 text-slate-300 tabular-nums">
                            {pos.fair_value != null ? `${pos.fair_value}¢` : '—'}
                          </td>

                          {/* Cost */}
                          <td className="py-3 pr-4 text-slate-300 tabular-nums">
                            ${cost.toFixed(2)}
                          </td>

                          {/* Unrealized P&L */}
                          <td className="py-3 pr-4 tabular-nums">
                            {pnl.positive === null ? (
                              <span className="text-slate-500">—</span>
                            ) : pnl.positive ? (
                              <span className="text-emerald-400">{pnl.text}</span>
                            ) : (
                              <span className="text-rose-400">{pnl.text}</span>
                            )}
                          </td>

                          {/* Time In Trade */}
                          <td className="py-3 pr-4 text-slate-500 tabular-nums whitespace-nowrap text-xs">
                            {timeInTrade}
                          </td>

                          {/* Close Time */}
                          <td className="py-3 pr-4 text-slate-400 whitespace-nowrap">
                            {formatCloseTime(pos.close_time)}
                          </td>

                          {/* Status */}
                          <td className="py-3">
                            {pos.fill_confirmed
                              ? <span className="badge-success">Filled</span>
                              : <span className="badge-muted">Pending</span>
                            }
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              /* Empty state */
              <div className="py-20 flex flex-col items-center gap-4 text-slate-500">
                <div className="rounded-full bg-surface-2 p-4">
                  <Inbox size={36} strokeWidth={1.25} className="text-slate-500" />
                </div>
                <div className="text-center space-y-1">
                  <p className="text-base font-semibold text-slate-400">No open positions</p>
                  <p className="text-sm text-slate-600">The bot has no active contracts right now.</p>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
