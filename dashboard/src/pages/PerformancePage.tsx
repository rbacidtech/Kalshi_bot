import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  TrendingUp, TrendingDown, BarChart2, Clock,
  Percent, Award, AlertTriangle, Inbox,
} from 'lucide-react'
import { api } from '../lib/api'

// ── Types ────────────────────────────────────────────────────────────────────

interface PerformanceSummary {
  period_days:        number
  total_trades:       number
  wins:               number
  losses:             number
  win_rate:           number
  total_pnl_cents:    number
  avg_pnl_per_trade:  number
  by_strategy:        Record<string, { trades: number; wins: number; pnl_cents: number }>
  best_trade:         { ticker: string; pnl_cents: number; strategy: string } | null
  worst_trade:        { ticker: string; pnl_cents: number; strategy: string } | null
  avg_hold_time_hours: number
  sharpe_daily:       number | null
}

type Period = 7 | 30 | 90

// ── Helpers ──────────────────────────────────────────────────────────────────

function centsToDisplay(cents: number): string {
  const dollars = Math.abs(cents) / 100
  return `$${dollars.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function pnlText(cents: number): string {
  return (cents >= 0 ? '+' : '-') + centsToDisplay(cents)
}

function pnlClass(cents: number): string {
  return cents >= 0 ? 'text-emerald-400' : 'text-rose-400'
}

function winRatePct(rate: number): string {
  return (rate * 100).toFixed(1) + '%'
}

// ── Win Rate Ring (SVG — no black-hole artifact) ─────────────────────────────

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

// ── Stat Card ────────────────────────────────────────────────────────────────

interface StatCardProps {
  label:   string
  value:   React.ReactNode
  icon?:   React.ReactNode
  sub?:    React.ReactNode
  accent?: string
}

function StatCard({ label, value, icon, sub, accent }: StatCardProps) {
  return (
    <div
      className="bg-surface-1 rounded-xl border border-border p-4 flex flex-col gap-1 min-w-0"
      style={accent ? {
        borderTopColor: accent,
        borderTopWidth: 3,
        boxShadow: `0 4px 24px ${accent}22`,
      } : undefined}
    >
      <span className="text-xs font-medium text-slate-500 uppercase tracking-wide truncate">{label}</span>
      <div className="flex items-center gap-2.5 mt-0.5">
        {icon && <span className="shrink-0">{icon}</span>}
        <span className="text-2xl font-bold truncate">{value}</span>
      </div>
      {sub && <div className="text-xs text-slate-500 mt-0.5">{sub}</div>}
    </div>
  )
}

// ── Skeleton ─────────────────────────────────────────────────────────────────

function StatSkeleton() {
  return (
    <div className="rounded-xl border border-border bg-surface-1 p-4 flex flex-col gap-2 animate-pulse">
      <div className="h-3 bg-surface-2 rounded w-24" />
      <div className="h-8 bg-surface-2 rounded w-32 mt-1" />
    </div>
  )
}

function StrategySkeleton() {
  return (
    <div className="space-y-3 mt-4">
      {[0, 1, 2].map(i => (
        <div key={i} className="flex gap-4 animate-pulse">
          <div className="h-5 bg-surface-2 rounded w-40" />
          <div className="h-5 bg-surface-2 rounded w-12" />
          <div className="h-5 bg-surface-2 rounded w-28" />
          <div className="h-5 bg-surface-2 rounded w-20" />
        </div>
      ))}
    </div>
  )
}

function TradeCardSkeleton() {
  return (
    <div className="rounded-xl border border-border bg-surface-1 p-4 animate-pulse">
      <div className="h-3 bg-surface-2 rounded w-20 mb-3" />
      <div className="h-7 bg-surface-2 rounded w-36 mb-2" />
      <div className="h-3 bg-surface-2 rounded w-24" />
    </div>
  )
}

// ── Strategy Row ─────────────────────────────────────────────────────────────

interface StrategyRowProps {
  name:      string
  trades:    number
  wins:      number
  pnl_cents: number
  maxAbsPnl: number
}

function StrategyRow({ name, trades, wins, pnl_cents, maxAbsPnl }: StrategyRowProps) {
  const wr       = trades > 0 ? wins / trades : 0
  const wrPct    = Math.round(wr * 100)
  const barWidth = maxAbsPnl > 0 ? Math.round((Math.abs(pnl_cents) / maxAbsPnl) * 100) : 0
  const isPos    = pnl_cents >= 0

  return (
    <tr className="hover:bg-surface-2/40 transition-colors">
      <td className="py-3 pr-4">
        <span className="font-mono text-sm text-slate-200">{name}</span>
      </td>
      <td className="py-3 pr-4 text-slate-400 tabular-nums text-sm">{trades}</td>
      <td className="py-3 pr-6">
        <div className="flex items-center gap-2">
          <div className="w-16 h-1.5 bg-surface-3 rounded-full overflow-hidden shrink-0">
            <div
              className={`h-full rounded-full ${wrPct >= 55 ? 'bg-emerald-400' : wrPct >= 45 ? 'bg-amber-400' : 'bg-rose-400'}`}
              style={{ width: `${wrPct}%` }}
            />
          </div>
          <span className="text-slate-300 text-xs tabular-nums w-9 shrink-0">{wrPct}%</span>
        </div>
      </td>
      <td className="py-3">
        <div className="flex items-center gap-2">
          <div className="w-20 h-1.5 bg-surface-3 rounded-full overflow-hidden shrink-0">
            <div
              className={`h-full rounded-full ${isPos ? 'bg-emerald-400' : 'bg-rose-400'}`}
              style={{ width: `${barWidth}%` }}
            />
          </div>
          <span className={`text-sm font-medium tabular-nums ${pnlClass(pnl_cents)}`}>
            {pnlText(pnl_cents)}
          </span>
        </div>
      </td>
    </tr>
  )
}

// ── Best / Worst Trade Card ───────────────────────────────────────────────────

function TradeHighlightCard({ type, trade }: {
  type:  'best' | 'worst'
  trade: { ticker: string; pnl_cents: number; strategy: string }
}) {
  const isBest    = type === 'best'
  const Icon      = isBest ? Award : AlertTriangle
  const color     = isBest ? '#34d399' : '#f87171'
  return (
    <div
      className="bg-surface-1 rounded-xl border border-border p-4 flex flex-col gap-2"
      style={{ borderTopColor: color, borderTopWidth: 3, boxShadow: `0 4px 24px ${color}18` }}
    >
      <div className="flex items-center gap-2">
        <Icon size={14} style={{ color }} />
        <span className="text-xs font-medium text-slate-500 uppercase tracking-wide">
          {isBest ? 'Best Trade' : 'Worst Trade'}
        </span>
      </div>
      <div className="flex items-center justify-between gap-4 mt-0.5">
        <span className="font-mono text-sm font-semibold text-slate-200 truncate">{trade.ticker}</span>
        <span className="text-xl font-bold tabular-nums shrink-0" style={{ color }}>
          {pnlText(trade.pnl_cents)}
        </span>
      </div>
      <span className="text-xs text-slate-500">
        Strategy: <span className="text-slate-400">{trade.strategy}</span>
      </span>
    </div>
  )
}

// ── Period Selector ───────────────────────────────────────────────────────────

const PERIODS: Period[] = [7, 30, 90]

function PeriodSelector({ value, onChange }: { value: Period; onChange: (p: Period) => void }) {
  return (
    <div className="flex items-center gap-1 bg-surface-2 border border-border rounded-full p-0.5">
      {PERIODS.map(p => (
        <button
          key={p}
          onClick={() => onChange(p)}
          className={`px-3.5 py-1.5 rounded-full text-xs font-semibold transition-all duration-150 ${
            value === p
              ? 'bg-gradient-to-r from-blue-600 to-indigo-600 text-white shadow-sm'
              : 'text-slate-400 hover:text-white'
          }`}
        >
          {p}d
        </button>
      ))}
    </div>
  )
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function PerformancePage() {
  const [days, setDays] = useState<Period>(30)

  const { data, isLoading, isError } = useQuery<PerformanceSummary>({
    queryKey: ['performance', days],
    queryFn:  () => api.get('/performance', { params: { days } }).then(r => r.data),
    staleTime: 60_000,
  })

  const isEmpty = !isLoading && !isError && data?.total_trades === 0

  const sortedStrategies: Array<[string, { trades: number; wins: number; pnl_cents: number }]> =
    data ? Object.entries(data.by_strategy).sort((a, b) => b[1].pnl_cents - a[1].pnl_cents) : []

  const maxAbsPnl = sortedStrategies.reduce((acc, [, v]) => Math.max(acc, Math.abs(v.pnl_cents)), 0)

  const totalPnlPos = (data?.total_pnl_cents ?? 0) >= 0
  const avgPnlPos   = (data?.avg_pnl_per_trade ?? 0) >= 0

  return (
    <div className="space-y-5">

      {/* ── Page Header ───────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-white">Performance Analytics</h1>
          <p className="text-xs text-slate-500 mt-0.5">Closed trade statistics</p>
        </div>
        <PeriodSelector value={days} onChange={setDays} />
      </div>

      {/* ── Stat Cards ────────────────────────────────────────────────────── */}
      {isLoading ? (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          {[0,1,2,3,4,5].map(i => <StatSkeleton key={i} />)}
        </div>
      ) : data ? (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">

          <StatCard
            label="Total P&L"
            accent={totalPnlPos ? '#34d399' : '#f87171'}
            icon={totalPnlPos
              ? <TrendingUp size={18} className="text-emerald-400" />
              : <TrendingDown size={18} className="text-rose-400" />}
            value={<span className={pnlClass(data.total_pnl_cents)}>{pnlText(data.total_pnl_cents)}</span>}
          />

          <StatCard
            label="Win Rate"
            accent={data.win_rate >= 0.55 ? '#34d399' : data.win_rate >= 0.45 ? '#fbbf24' : '#f87171'}
            icon={<WinRateRing rate={data.win_rate} />}
            value={
              <span className={data.win_rate >= 0.55 ? 'text-emerald-400' : data.win_rate >= 0.45 ? 'text-amber-400' : 'text-rose-400'}>
                {winRatePct(data.win_rate)}
              </span>
            }
            sub={`${data.wins}W / ${data.losses}L`}
          />

          <StatCard
            label="Total Trades"
            accent="#60a5fa"
            icon={<BarChart2 size={18} className="text-blue-400" />}
            value={<span className="text-white">{data.total_trades.toLocaleString()}</span>}
          />

          <StatCard
            label="Avg P&L / Trade"
            accent={avgPnlPos ? '#34d399' : '#f87171'}
            icon={avgPnlPos
              ? <TrendingUp size={18} className="text-emerald-400" />
              : <TrendingDown size={18} className="text-rose-400" />}
            value={<span className={pnlClass(data.avg_pnl_per_trade)}>{pnlText(data.avg_pnl_per_trade)}</span>}
          />

          <StatCard
            label="Avg Hold Time"
            accent="#94a3b8"
            icon={<Clock size={18} className="text-slate-400" />}
            value={
              <span className="text-white">
                {data.avg_hold_time_hours >= 24
                  ? `${(data.avg_hold_time_hours / 24).toFixed(1)}d`
                  : `${data.avg_hold_time_hours.toFixed(1)}h`}
              </span>
            }
          />

          <StatCard
            label="Sharpe Ratio"
            accent={data.sharpe_daily != null ? (data.sharpe_daily >= 1 ? '#34d399' : data.sharpe_daily >= 0 ? '#fbbf24' : '#f87171') : '#94a3b8'}
            icon={<Percent size={18} className="text-slate-400" />}
            value={
              data.sharpe_daily != null
                ? <span className={data.sharpe_daily >= 1 ? 'text-emerald-400' : data.sharpe_daily >= 0 ? 'text-amber-400' : 'text-rose-400'}>
                    {data.sharpe_daily.toFixed(2)}
                  </span>
                : <span className="text-slate-500">N/A</span>
            }
          />
        </div>
      ) : null}

      {/* ── Error ─────────────────────────────────────────────────────────── */}
      {isError && (
        <div
          className="rounded-xl border py-10 text-center text-rose-400 text-sm"
          style={{ background: 'rgba(248,113,113,0.05)', borderColor: 'rgba(248,113,113,0.2)' }}
        >
          Failed to load performance data. Please try again.
        </div>
      )}

      {/* ── Empty ─────────────────────────────────────────────────────────── */}
      {isEmpty && (
        <div className="card py-16 flex flex-col items-center gap-3 text-slate-500">
          <Inbox size={36} strokeWidth={1.25} />
          <p className="text-sm">No completed trades in the last {days} days</p>
        </div>
      )}

      {/* ── Strategy Breakdown ────────────────────────────────────────────── */}
      {!isEmpty && (
        <div
          className="rounded-xl border border-border bg-surface-1 p-5"
          style={{ borderTopColor: '#60a5fa', borderTopWidth: 3, boxShadow: '0 4px 24px rgba(96,165,250,0.08)' }}
        >
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-sm font-semibold text-slate-300">Strategy Breakdown</h2>
            {data && (
              <span className="text-xs text-slate-500">
                {sortedStrategies.length} strategies · {days}d window
              </span>
            )}
          </div>

          {isLoading ? (
            <StrategySkeleton />
          ) : data && sortedStrategies.length > 0 ? (
            <div className="overflow-x-auto -mx-5 px-5">
              <table className="w-full text-sm min-w-[480px]">
                <thead>
                  <tr className="border-b border-border text-left">
                    {['Strategy', 'Trades', 'Win Rate', 'P&L'].map(col => (
                      <th key={col} className="pb-2.5 pr-6 text-xs font-medium text-slate-500 whitespace-nowrap last:pr-0">
                        {col}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-border/50">
                  {sortedStrategies.map(([name, stats]) => (
                    <StrategyRow
                      key={name}
                      name={name}
                      trades={stats.trades}
                      wins={stats.wins}
                      pnl_cents={stats.pnl_cents}
                      maxAbsPnl={maxAbsPnl}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            !isLoading && (
              <div className="py-8 text-center text-slate-500 text-sm">No strategy data available</div>
            )
          )}
        </div>
      )}

      {/* ── Best / Worst Trade ────────────────────────────────────────────── */}
      {!isEmpty && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {isLoading ? (
            <><TradeCardSkeleton /><TradeCardSkeleton /></>
          ) : data ? (
            <>
              {data.best_trade
                ? <TradeHighlightCard type="best"  trade={data.best_trade} />
                : <div className="card flex items-center justify-center text-slate-500 text-sm py-6">No best trade data</div>
              }
              {data.worst_trade
                ? <TradeHighlightCard type="worst" trade={data.worst_trade} />
                : <div className="card flex items-center justify-center text-slate-500 text-sm py-6">No worst trade data</div>
              }
            </>
          ) : null}
        </div>
      )}
    </div>
  )
}
