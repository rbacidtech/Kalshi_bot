import { useState, type ReactNode } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Users,
  UserCheck,
  DollarSign,
  Shield,
  Settings,
  Trash2,
  ChevronLeft,
  ChevronRight,
  Search,
  Loader2,
} from 'lucide-react'
import { admin } from '../lib/api'
import type { User } from '../lib/auth'

// ── Types ──────────────────────────────────────────────────────────────────

interface AdminStats {
  total_users: number
  active_users: number
  users_by_tier: { free: number; starter: number; pro: number; institutional: number }
  total_deployed_cents: number
}

interface UsersResponse {
  users: User[]
  total: number
  page: number
  per_page: number
}

// ── Constants ──────────────────────────────────────────────────────────────

const TIERS = ['free', 'starter', 'pro', 'institutional'] as const
type Tier = (typeof TIERS)[number]
const PER_PAGE = 20

// ── Helpers ────────────────────────────────────────────────────────────────

function formatDate(iso: string | null): string {
  if (!iso) return 'Never'
  return new Date(iso).toLocaleDateString('en-US', {
    year:  'numeric',
    month: 'short',
    day:   'numeric',
  })
}

function truncateEmail(email: string, maxLen = 30): string {
  if (email.length <= maxLen) return email
  return email.slice(0, maxLen - 1) + '\u2026'
}

// ── Tier badge ─────────────────────────────────────────────────────────────

function TierBadge({ tier }: { tier: string }) {
  if (tier === 'free')          return <span className="badge-muted capitalize">{tier}</span>
  if (tier === 'starter')       return <span className="badge-blue capitalize">{tier}</span>
  if (tier === 'pro')           return <span className="badge-success capitalize">{tier}</span>
  if (tier === 'institutional') return (
    <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-violet-900/30 text-violet-300">
      Institutional
    </span>
  )
  return <span className="badge-muted capitalize">{tier}</span>
}

// ── Tier proportion bar ───────────────────────────────────────────────────

interface TierBarProps { counts: AdminStats['users_by_tier'] }

function TierBar({ counts }: TierBarProps) {
  const total = (counts.free + counts.starter + counts.pro + counts.institutional) || 1

  const segments: { key: Tier; label: string; barCls: string; textCls: string }[] = [
    { key: 'free',          label: 'Free',          barCls: 'bg-slate-600',   textCls: 'text-slate-400'   },
    { key: 'starter',       label: 'Starter',       barCls: 'bg-blue-500',    textCls: 'text-blue-400'    },
    { key: 'pro',           label: 'Pro',            barCls: 'bg-emerald-500', textCls: 'text-emerald-400' },
    { key: 'institutional', label: 'Institutional',  barCls: 'bg-violet-500',  textCls: 'text-violet-400'  },
  ]

  return (
    <div className="mt-2">
      {/* Segmented bar */}
      <div className="flex h-2 w-full rounded-full overflow-hidden bg-surface-2 gap-px">
        {segments.map(({ key, barCls }) => {
          const pct = (counts[key] / total) * 100
          return (
            <div
              key={key}
              className={`${barCls} transition-all duration-500`}
              style={{ width: `${pct}%`, minWidth: pct > 0 ? '3px' : '0' }}
            />
          )
        })}
      </div>
      {/* Count labels */}
      <div className="flex flex-wrap gap-x-3 gap-y-1 mt-2">
        {segments.map(({ key, label, textCls }) => (
          <div key={key} className="flex items-center gap-1">
            <span className={`text-xs font-medium ${textCls}`}>{label}</span>
            <span className="text-xs text-muted">{counts[key]}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Stat card ─────────────────────────────────────────────────────────────

function StatCard({
  icon,
  label,
  value,
  children,
}: {
  icon: ReactNode
  label: string
  value?: ReactNode
  children?: ReactNode
}) {
  return (
    <div className="card flex flex-col gap-3">
      <div className="flex items-center gap-2 text-muted">
        {icon}
        <span className="text-xs font-medium uppercase tracking-wide">{label}</span>
      </div>
      {value !== undefined && (
        <p className="text-2xl font-semibold text-slate-100 leading-none">{value}</p>
      )}
      {children}
    </div>
  )
}

// ── Skeleton rows ─────────────────────────────────────────────────────────

function SkeletonRows({ count = 8 }: { count?: number }) {
  return (
    <>
      {Array.from({ length: count }).map((_, i) => (
        <tr key={i} className="border-t border-border animate-pulse">
          <td className="px-4 py-3"><div className="h-3 w-44 rounded bg-surface-3" /></td>
          <td className="px-4 py-3"><div className="h-3 w-16 rounded bg-surface-3" /></td>
          <td className="px-4 py-3"><div className="h-3 w-14 rounded bg-surface-3" /></td>
          <td className="px-4 py-3"><div className="h-3 w-5  rounded bg-surface-3" /></td>
          <td className="px-4 py-3"><div className="h-3 w-24 rounded bg-surface-3" /></td>
          <td className="px-4 py-3"><div className="h-3 w-16 rounded bg-surface-3" /></td>
        </tr>
      ))}
    </>
  )
}

// ── Inline edit panel ─────────────────────────────────────────────────────

interface EditPanelProps {
  user:    User
  onClose: () => void
  onSaved: () => void
}

function EditPanel({ user, onClose, onSaved }: EditPanelProps) {
  const queryClient = useQueryClient()

  const [tier,     setTier]     = useState<string>(user.tier)
  const [isAdmin,  setIsAdmin]  = useState(user.is_admin)
  const [isActive, setIsActive] = useState(user.is_active)

  const mutation = useMutation({
    mutationFn: () =>
      admin.updateUser(user.id, { tier, is_admin: isAdmin, is_active: isActive }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin-users'] })
      onSaved()
    },
  })

  return (
    <tr className="border-t border-border bg-surface-0">
      <td colSpan={6} className="px-4 py-4">
        <div className="card p-4 max-w-sm space-y-4">
          <p className="text-xs font-semibold text-muted uppercase tracking-wide">
            Edit &mdash;{' '}
            <span className="text-slate-300 normal-case font-mono">{user.email}</span>
          </p>

          {/* Tier select */}
          <div>
            <label className="label" htmlFor={`tier-${user.id}`}>Tier</label>
            <select
              id={`tier-${user.id}`}
              className="input"
              value={tier}
              onChange={e => setTier(e.target.value)}
            >
              {TIERS.map(t => (
                <option key={t} value={t} className="bg-surface-2">
                  {t.charAt(0).toUpperCase() + t.slice(1)}
                </option>
              ))}
            </select>
          </div>

          {/* Toggles */}
          <div className="flex flex-col gap-3">
            <label className="flex items-center gap-3 cursor-pointer select-none">
              <input
                type="checkbox"
                className="w-4 h-4 rounded accent-blue-500"
                checked={isAdmin}
                onChange={e => setIsAdmin(e.target.checked)}
              />
              <span className="text-sm text-slate-300">Admin access</span>
            </label>

            <label className="flex items-center gap-3 cursor-pointer select-none">
              <input
                type="checkbox"
                className="w-4 h-4 rounded accent-blue-500"
                checked={isActive}
                onChange={e => setIsActive(e.target.checked)}
              />
              <span className="text-sm text-slate-300">Active</span>
            </label>
          </div>

          {/* Error */}
          {mutation.isError && (
            <p className="text-xs text-danger">Save failed. Please try again.</p>
          )}

          {/* Buttons */}
          <div className="flex items-center gap-2 pt-1">
            <button
              className="btn-primary py-1.5 text-xs"
              onClick={() => mutation.mutate()}
              disabled={mutation.isPending}
            >
              {mutation.isPending && <Loader2 size={13} className="animate-spin" />}
              {mutation.isPending ? 'Saving\u2026' : 'Save'}
            </button>
            <button className="btn-ghost py-1.5 text-xs" onClick={onClose}>
              Cancel
            </button>
          </div>
        </div>
      </td>
    </tr>
  )
}

// ── Page ──────────────────────────────────────────────────────────────────

export default function AdminPage() {
  const queryClient = useQueryClient()

  const [page,      setPage]      = useState(1)
  const [search,    setSearch]    = useState('')
  const [editingId, setEditingId] = useState<string | null>(null)

  // Stats
  const statsQuery = useQuery<AdminStats>({
    queryKey: ['admin-stats'],
    queryFn:  async () => {
      const { data } = await admin.stats()
      return data
    },
  })

  // Users (server-paginated)
  const usersQuery = useQuery<UsersResponse>({
    queryKey: ['admin-users', page],
    queryFn:  async () => {
      const { data } = await admin.users(page, PER_PAGE)
      return data
    },
    placeholderData: prev => prev,
  })

  // Delete
  const deleteMutation = useMutation({
    mutationFn: (id: string) => admin.deleteUser(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin-users'] })
      queryClient.invalidateQueries({ queryKey: ['admin-stats'] })
    },
  })

  function handleDelete(user: User) {
    if (!window.confirm(`Delete user "${user.email}"? This cannot be undone.`)) return
    deleteMutation.mutate(user.id)
  }

  // Local search filter over the current page's users
  const allUsers = usersQuery.data?.users ?? []
  const filtered = search.trim()
    ? allUsers.filter(u => u.email.toLowerCase().includes(search.trim().toLowerCase()))
    : allUsers

  const total      = usersQuery.data?.total ?? 0
  const totalPages = Math.max(1, Math.ceil(total / PER_PAGE))
  const rangeStart = (page - 1) * PER_PAGE + 1
  const rangeEnd   = Math.min(page * PER_PAGE, total)

  const stats = statsQuery.data

  return (
    <div className="space-y-6 max-w-7xl mx-auto">

      {/* ── Section 1: Platform Stats ─────────────────────────────────── */}
      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">

        {/* Total Users */}
        <StatCard
          icon={<Users size={15} strokeWidth={1.75} />}
          label="Total Users"
          value={
            statsQuery.isLoading ? (
              <span className="inline-block w-16 h-6 rounded bg-surface-3 animate-pulse align-middle" />
            ) : statsQuery.isError ? '—' : (
              stats?.total_users.toLocaleString()
            )
          }
        />

        {/* Active Users */}
        <StatCard
          icon={<UserCheck size={15} strokeWidth={1.75} />}
          label="Active Users"
          value={
            statsQuery.isLoading ? (
              <span className="inline-block w-16 h-6 rounded bg-surface-3 animate-pulse align-middle" />
            ) : statsQuery.isError ? '—' : (
              stats?.active_users.toLocaleString()
            )
          }
        />

        {/* Total Deployed */}
        <StatCard
          icon={<DollarSign size={15} strokeWidth={1.75} />}
          label="Total Deployed"
          value={
            statsQuery.isLoading ? (
              <span className="inline-block w-28 h-6 rounded bg-surface-3 animate-pulse align-middle" />
            ) : statsQuery.isError || !stats ? '—' : (
              `$${(stats.total_deployed_cents / 100).toLocaleString('en-US', {
                minimumFractionDigits: 2,
                maximumFractionDigits: 2,
              })}`
            )
          }
        />

        {/* Tier breakdown */}
        <StatCard
          icon={<Users size={15} strokeWidth={1.75} />}
          label="By Tier"
        >
          {statsQuery.isLoading ? (
            <div className="space-y-2">
              <div className="h-2 w-full rounded-full bg-surface-3 animate-pulse" />
              <div className="h-3 w-36 rounded bg-surface-3 animate-pulse" />
            </div>
          ) : statsQuery.isError || !stats ? (
            <p className="text-sm text-muted">Unavailable</p>
          ) : (
            <TierBar counts={stats.users_by_tier} />
          )}
        </StatCard>
      </div>

      {/* ── Section 2: User Management ───────────────────────────────── */}
      <div className="card p-0 overflow-hidden">

        {/* Header */}
        <div className="flex items-center justify-between gap-4 px-5 py-4 border-b border-border">
          <div className="flex items-center gap-2.5">
            <h2 className="text-sm font-semibold text-slate-100">Users</h2>
            {usersQuery.isSuccess && (
              <span className="badge-muted">{total.toLocaleString()}</span>
            )}
          </div>

          {/* Search */}
          <div className="relative w-56">
            <Search
              size={14}
              className="absolute left-3 top-1/2 -translate-y-1/2 text-muted pointer-events-none"
              aria-hidden="true"
            />
            <input
              type="search"
              placeholder="Filter by email\u2026"
              className="input pl-8 py-1.5 text-xs"
              value={search}
              onChange={e => { setSearch(e.target.value); setPage(1) }}
            />
          </div>
        </div>

        {/* Error state */}
        {usersQuery.isError && (
          <div className="px-5 py-12 text-center text-sm text-danger">
            Failed to load users. Please refresh the page.
          </div>
        )}

        {/* Table */}
        {!usersQuery.isError && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm min-w-[640px]">
              <thead>
                <tr className="text-left border-b border-border">
                  <th className="px-4 py-3 text-xs font-medium text-muted uppercase tracking-wide whitespace-nowrap">Email</th>
                  <th className="px-4 py-3 text-xs font-medium text-muted uppercase tracking-wide whitespace-nowrap">Tier</th>
                  <th className="px-4 py-3 text-xs font-medium text-muted uppercase tracking-wide whitespace-nowrap">Status</th>
                  <th className="px-4 py-3 text-xs font-medium text-muted uppercase tracking-wide whitespace-nowrap">Admin</th>
                  <th className="px-4 py-3 text-xs font-medium text-muted uppercase tracking-wide whitespace-nowrap">Last Login</th>
                  <th className="px-4 py-3 text-xs font-medium text-muted uppercase tracking-wide whitespace-nowrap">Actions</th>
                </tr>
              </thead>
              <tbody>
                {usersQuery.isLoading ? (
                  <SkeletonRows count={8} />
                ) : filtered.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="px-4 py-12 text-center text-sm text-muted">
                      {search ? 'No users match your search.' : 'No users found.'}
                    </td>
                  </tr>
                ) : (
                  filtered.map(user => (
                    <>
                      {/* User row */}
                      <tr
                        key={user.id}
                        className="border-t border-border hover:bg-surface-2/40 transition-colors duration-100"
                      >
                        {/* Email */}
                        <td className="px-4 py-3">
                          <span
                            className="font-mono text-sm text-slate-300"
                            title={user.email}
                          >
                            {truncateEmail(user.email)}
                          </span>
                        </td>

                        {/* Tier */}
                        <td className="px-4 py-3">
                          <TierBadge tier={user.tier} />
                        </td>

                        {/* Status */}
                        <td className="px-4 py-3">
                          {user.is_active
                            ? <span className="badge-success">Active</span>
                            : <span className="badge-danger">Inactive</span>
                          }
                        </td>

                        {/* Admin shield */}
                        <td className="px-4 py-3">
                          <Shield
                            size={15}
                            strokeWidth={user.is_admin ? 0 : 1.75}
                            fill={user.is_admin ? 'currentColor' : 'none'}
                            className={user.is_admin ? 'text-accent-blue' : 'text-muted'}
                            aria-label={user.is_admin ? 'Admin' : 'Not admin'}
                          />
                        </td>

                        {/* Last login */}
                        <td className="px-4 py-3 text-slate-400 tabular-nums whitespace-nowrap">
                          {formatDate(user.last_login_at)}
                        </td>

                        {/* Actions */}
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-1">
                            {/* Edit */}
                            <button
                              className={[
                                'btn-ghost px-2 py-1.5',
                                editingId === user.id
                                  ? 'text-accent-blue bg-accent-blue/10'
                                  : '',
                              ].join(' ')}
                              title="Edit user"
                              aria-label="Edit user"
                              onClick={() =>
                                setEditingId(prev => prev === user.id ? null : user.id)
                              }
                            >
                              <Settings size={14} strokeWidth={1.75} />
                            </button>

                            {/* Delete */}
                            <button
                              className="btn-ghost px-2 py-1.5 hover:text-danger hover:bg-danger/10"
                              title="Delete user"
                              aria-label="Delete user"
                              disabled={deleteMutation.isPending && deleteMutation.variables === user.id}
                              onClick={() => handleDelete(user)}
                            >
                              {deleteMutation.isPending && deleteMutation.variables === user.id ? (
                                <Loader2 size={14} className="animate-spin text-danger" />
                              ) : (
                                <Trash2 size={14} strokeWidth={1.75} />
                              )}
                            </button>
                          </div>
                        </td>
                      </tr>

                      {/* Inline edit panel — expands below the row */}
                      {editingId === user.id && (
                        <EditPanel
                          key={`edit-${user.id}`}
                          user={user}
                          onClose={() => setEditingId(null)}
                          onSaved={() => setEditingId(null)}
                        />
                      )}
                    </>
                  ))
                )}
              </tbody>
            </table>
          </div>
        )}

        {/* Pagination */}
        {usersQuery.isSuccess && total > 0 && (
          <div className="flex items-center justify-between gap-4 px-5 py-3 border-t border-border">
            <p className="text-xs text-muted">
              Showing{' '}
              <span className="text-slate-300 font-medium">{rangeStart}&ndash;{rangeEnd}</span>
              {' '}of{' '}
              <span className="text-slate-300 font-medium">{total}</span>{' '}users
            </p>

            <div className="flex items-center gap-1">
              <button
                className="btn-ghost px-2 py-1.5 disabled:opacity-40"
                disabled={page <= 1}
                aria-label="Previous page"
                onClick={() => setPage(p => Math.max(1, p - 1))}
              >
                <ChevronLeft size={14} />
              </button>

              <span className="text-xs text-muted px-2 tabular-nums">
                {page} / {totalPages}
              </span>

              <button
                className="btn-ghost px-2 py-1.5 disabled:opacity-40"
                disabled={page >= totalPages}
                aria-label="Next page"
                onClick={() => setPage(p => Math.min(totalPages, p + 1))}
              >
                <ChevronRight size={14} />
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
