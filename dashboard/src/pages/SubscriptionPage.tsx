import { useQuery } from '@tanstack/react-query'
import { subscriptions } from '../lib/api'

// ── Types ─────────────────────────────────────────────────────────────────────

interface SubscriptionMe {
  tier: string
  volume_limit_cents: number
  current_month_volume_cents: number
  billing_cycle_start: string
  is_active: boolean
  volume_used_pct: number
}

// ── Constants ─────────────────────────────────────────────────────────────────

const TIER_ORDER: string[] = ['free', 'starter', 'pro', 'institutional']

const TIER_PILL: Record<string, string> = {
  free:          'bg-surface-3 text-muted border border-border',
  starter:       'bg-accent-blue/10 text-accent-blue border border-accent-blue/30',
  pro:           'bg-success/10 text-success border border-success/30',
  institutional: 'bg-violet-500/10 text-violet-400 border border-violet-500/30',
}

const TIER_ACCENT: Record<string, string> = {
  free:          '#94a3b8',
  starter:       '#60a5fa',
  pro:           '#34d399',
  institutional: '#a78bfa',
}

const TIER_LABEL: Record<string, string> = {
  free:          'FREE',
  starter:       'STARTER',
  pro:           'PRO',
  institutional: 'INSTITUTIONAL',
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function centsToDisplay(cents: number): string {
  return '$' + (cents / 100).toLocaleString('en-US', { maximumFractionDigits: 0 })
}

function fmtCycleDate(iso: string): string {
  // billing_cycle_start may be a plain day-of-month integer ("15") or an ISO date
  const asNum = parseInt(iso, 10)
  if (!isNaN(asNum) && String(asNum) === iso.trim()) {
    const d = new Date()
    d.setDate(asNum)
    return d.toLocaleDateString('en-US', { month: 'long', day: 'numeric' })
  }
  return new Date(iso).toLocaleDateString('en-US', {
    year: 'numeric', month: 'long', day: 'numeric',
  })
}

function barColor(pct: number): string {
  if (pct >= 90) return 'bg-danger'
  if (pct >= 70) return 'bg-warning'
  return 'bg-success'
}

// ── Volume meter ──────────────────────────────────────────────────────────────

function VolumeMeter({ sub }: { sub: SubscriptionMe }) {
  const isUnlimited = sub.volume_limit_cents === 0

  if (isUnlimited) {
    return (
      <div>
        <p className="label mb-1">Monthly Volume Used</p>
        <p className="text-sm text-muted">
          Unlimited —{' '}
          <span className="text-slate-300 font-medium">
            {centsToDisplay(sub.current_month_volume_cents)}
          </span>{' '}
          used this month
        </p>
      </div>
    )
  }

  const pct = Math.min(sub.volume_used_pct ?? 0, 100)

  return (
    <div>
      <p className="label mb-2">Monthly Volume Used</p>

      <div className="h-2 w-full rounded-full bg-surface-3 overflow-hidden">
        <div
          className={['h-full rounded-full transition-all duration-500', barColor(pct)].join(' ')}
          style={{ width: `${pct}%` }}
        />
      </div>

      <div className="flex items-center justify-between mt-1.5 text-xs text-muted">
        <span>
          <span className="text-slate-300 font-medium">
            {centsToDisplay(sub.current_month_volume_cents)}
          </span>
          {' used'}
        </span>
        <span>
          limit{' '}
          <span className="text-slate-300 font-medium">
            {centsToDisplay(sub.volume_limit_cents)}
          </span>
        </span>
      </div>
    </div>
  )
}

function VolumeMeterSkeleton() {
  return (
    <div className="animate-pulse space-y-2">
      <div className="h-2.5 w-36 rounded bg-surface-3" />
      <div className="h-2 w-full rounded-full bg-surface-3" />
      <div className="flex justify-between">
        <div className="h-2.5 w-20 rounded bg-surface-3" />
        <div className="h-2.5 w-20 rounded bg-surface-3" />
      </div>
    </div>
  )
}

// ── Tier table ────────────────────────────────────────────────────────────────

interface TableRowDef {
  label: string
  values: Record<string, string>
}

const TABLE_ROWS: TableRowDef[] = [
  {
    label: 'Monthly Volume',
    values: {
      free:          '$500',
      starter:       '$5,000',
      pro:           '$50,000',
      institutional: 'Unlimited',
    },
  },
  {
    label: 'Signals',
    values: {
      free:          'Shared',
      starter:       'Shared',
      pro:           'Shared',
      institutional: 'Shared',
    },
  },
  {
    label: 'API Keys',
    values: {
      free:          '✓',
      starter:       '✓',
      pro:           '✓',
      institutional: '✓',
    },
  },
  {
    label: 'Priority Exec',
    values: {
      free:          '—',
      starter:       '—',
      pro:           '✓',
      institutional: '✓',
    },
  },
]

function TierTable({ currentTier }: { currentTier: string }) {
  const tierIndex = TIER_ORDER.indexOf(currentTier)

  return (
    <div className="card">
      <h2 className="text-slate-100 font-semibold text-sm mb-4">Plans</h2>

      <div className="overflow-x-auto -mx-5 px-5">
        <table className="w-full text-sm border-collapse min-w-[480px]">
          <thead>
            <tr>
              <th className="w-32 pb-3 text-left" />
              {TIER_ORDER.map(tier => {
                const isActive = tier === currentTier
                const isHigher = TIER_ORDER.indexOf(tier) > tierIndex
                return (
                  <th
                    key={tier}
                    className={[
                      'pb-3 px-3 text-center rounded-t-lg border-x border-t',
                      isActive
                        ? 'bg-surface-1 border-border text-slate-200'
                        : 'border-transparent text-muted',
                    ].join(' ')}
                    style={isActive ? { borderTopColor: TIER_ACCENT[tier], borderTopWidth: 3 } : undefined}
                  >
                    <div className="flex flex-col items-center gap-1.5">
                      <span
                        className={[
                          'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-semibold tracking-wide',
                          isActive
                            ? TIER_PILL[tier]
                            : 'bg-surface-2 text-muted border border-border',
                        ].join(' ')}
                      >
                        {TIER_LABEL[tier] ?? tier.toUpperCase()}
                      </span>
                      {isActive && (
                        <span className="text-[10px] text-accent-blue font-medium">Current</span>
                      )}
                      {isHigher && (
                        <button
                          type="button"
                          className="text-[10px] px-2 py-0.5 rounded bg-surface-2 border border-border text-muted cursor-default opacity-60"
                          title="Contact support to upgrade"
                          disabled
                        >
                          Upgrade
                        </button>
                      )}
                    </div>
                  </th>
                )
              })}
            </tr>
          </thead>

          <tbody>
            {TABLE_ROWS.map((row, rowIdx) => {
              const isLast = rowIdx === TABLE_ROWS.length - 1
              return (
                <tr key={row.label} className="border-t border-border">
                  <td className="py-2.5 pr-3 text-xs text-muted font-medium">{row.label}</td>
                  {TIER_ORDER.map(tier => {
                    const isActive = tier === currentTier
                    const val      = row.values[tier] ?? '—'
                    const isCheck  = val === '✓'
                    const isDash   = val === '—'
                    return (
                      <td
                        key={tier}
                        className={[
                          'py-2.5 px-3 text-center text-xs border-x',
                          isLast ? 'rounded-b-lg border-b' : '',
                          isActive ? 'bg-surface-1 border-border' : 'border-transparent',
                        ].join(' ')}
                      >
                        <span
                          className={
                            isCheck
                              ? 'text-success font-semibold'
                              : isDash
                                ? 'text-surface-3'
                                : 'text-slate-300'
                          }
                        >
                          {val}
                        </span>
                      </td>
                    )
                  })}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function SubscriptionPage() {
  const subQuery = useQuery({
    queryKey: ['subscription'],
    queryFn:  async () => {
      const res = await subscriptions.me()
      return res.data as SubscriptionMe
    },
  })

  const tiersQuery = useQuery({
    queryKey: ['tiers'],
    queryFn:  async () => {
      const res = await subscriptions.tiers()
      return res.data
    },
  })

  const sub       = subQuery.data
  const tier      = sub?.tier ?? 'free'
  const isLoading = subQuery.isLoading

  return (
    <div className="max-w-2xl space-y-6">
      {/* Page heading */}
      <div>
        <h1 className="text-xl font-semibold text-slate-100">Subscription</h1>
        <p className="text-sm text-muted mt-1">
          Your current plan and usage for this billing cycle.
        </p>
      </div>

      {/* ── Current plan card ──────────────────────────────────────────────── */}
      <div
        className="bg-surface-1 border border-border rounded-xl p-5 space-y-5"
        style={{
          borderTopColor: TIER_ACCENT[tier] ?? '#60a5fa',
          borderTopWidth: 3,
          boxShadow: `0 4px 24px ${TIER_ACCENT[tier] ?? '#60a5fa'}18`,
        }}
      >
        {/* Tier pill */}
        <div className="flex items-center gap-3">
          {isLoading ? (
            <div className="h-7 w-28 rounded-full bg-surface-3 animate-pulse" />
          ) : (
            <span
              className={[
                'inline-flex items-center px-3.5 py-1 rounded-full text-sm font-semibold tracking-wider',
                TIER_PILL[tier] ?? 'bg-surface-3 text-muted border border-border',
              ].join(' ')}
            >
              {TIER_LABEL[tier] ?? tier.toUpperCase()}
            </span>
          )}

          {!isLoading && sub?.is_active && (
            <span className="badge-success text-[10px] py-0.5">Active</span>
          )}
          {!isLoading && sub && !sub.is_active && (
            <span className="badge-danger text-[10px] py-0.5">Inactive</span>
          )}
        </div>

        {/* Volume meter */}
        {isLoading
          ? <VolumeMeterSkeleton />
          : sub && <VolumeMeter sub={sub} />
        }

        {/* Billing cycle reset */}
        {isLoading ? (
          <div className="h-2.5 w-48 rounded bg-surface-3 animate-pulse" />
        ) : sub?.billing_cycle_start ? (
          <p className="text-xs text-muted">
            Resets on{' '}
            <span className="text-slate-300 font-medium">
              {fmtCycleDate(sub.billing_cycle_start)}
            </span>
          </p>
        ) : null}
      </div>

      {/* ── Tier comparison table ──────────────────────────────────────────── */}
      {tiersQuery.isLoading ? (
        <div className="card animate-pulse space-y-3">
          <div className="h-3 w-16 rounded bg-surface-3" />
          {[...Array(4)].map((_, i) => (
            <div key={i} className="h-2.5 w-full rounded bg-surface-3" />
          ))}
        </div>
      ) : (
        <TierTable currentTier={tier} />
      )}
    </div>
  )
}
