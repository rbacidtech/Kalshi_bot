import { useState, useEffect, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Key, CheckCircle, XCircle, Loader2, Plus, Trash2, Zap,
} from 'lucide-react'
import { keys } from '../lib/api'

// ── Types ─────────────────────────────────────────────────────────────────────

interface ApiKey {
  id: string
  exchange: string
  created_at: string
  last_used_at: string | null
}

type ToastState = { type: 'success' | 'error'; message: string } | null

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return 'Never'
  return new Date(iso).toLocaleDateString('en-US', {
    year: 'numeric', month: 'short', day: 'numeric',
  })
}

// ── Toast (inline, absolute-positioned within card) ───────────────────────────

function Toast({ toast }: { toast: ToastState }) {
  if (!toast) return null
  const isSuccess = toast.type === 'success'
  return (
    <div
      className={[
        'absolute top-4 right-4 z-10 flex items-center gap-2 px-3 py-2 rounded-lg border text-sm font-medium',
        'shadow-lg animate-fadeIn pointer-events-none',
        isSuccess
          ? 'bg-success/10 border-success/30 text-success'
          : 'bg-danger/10  border-danger/30  text-danger',
      ].join(' ')}
    >
      {isSuccess
        ? <CheckCircle size={14} strokeWidth={2} />
        : <XCircle     size={14} strokeWidth={2} />
      }
      {toast.message}
    </div>
  )
}

// ── useToast ─────────────────────────────────────────────────────────────────

function useToast() {
  const [toast, setToast] = useState<ToastState>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  function show(type: 'success' | 'error', message: string) {
    if (timerRef.current) clearTimeout(timerRef.current)
    setToast({ type, message })
    timerRef.current = setTimeout(() => setToast(null), 3000)
  }

  useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current) }, [])

  return { toast, show }
}

// ── Exchange config ───────────────────────────────────────────────────────────

interface ExchangeConfig {
  id: string
  label: string
  keyIdLabel: string
  keyIdPlaceholder: string
  dotColor: string
}

const EXCHANGES: ExchangeConfig[] = [
  {
    id: 'kalshi',
    label: 'Kalshi',
    keyIdLabel: 'API Key ID',
    keyIdPlaceholder: 'e.g. xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx',
    dotColor: 'bg-violet-400',
  },
  {
    id: 'coinbase',
    label: 'Coinbase',
    keyIdLabel: 'API Key Name',
    keyIdPlaceholder: 'e.g. organizations/xxx/apiKeys/xxx',
    dotColor: 'bg-blue-400',
  },
]

// ── ExchangeCard ──────────────────────────────────────────────────────────────

interface ExchangeCardProps {
  config: ExchangeConfig
  existingKey: ApiKey | undefined
}

function ExchangeCard({ config, existingKey }: ExchangeCardProps) {
  const qc = useQueryClient()
  const { toast, show } = useToast()

  const [formOpen, setFormOpen]     = useState(false)
  const [keyId,    setKeyId]        = useState('')
  const [privKey,  setPrivKey]      = useState('')

  // ── Mutations ──────────────────────────────────────────────────────────────

  const storeMut = useMutation({
    mutationFn: () => keys.store(config.id, keyId, privKey),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['keys'] })
      setFormOpen(false)
      setKeyId('')
      setPrivKey('')
      show('success', 'Credentials saved')
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      show('error', err.response?.data?.detail ?? 'Failed to save credentials')
    },
  })

  const removeMut = useMutation({
    mutationFn: () => keys.remove(config.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['keys'] })
      show('success', 'Key removed')
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      show('error', err.response?.data?.detail ?? 'Failed to remove key')
    },
  })

  const verifyMut = useMutation({
    mutationFn: () => keys.verify(config.id),
    onSuccess: (res) => {
      const { ok, message } = res.data as { ok: boolean; message: string }
      show(ok ? 'success' : 'error', message ?? (ok ? 'Connected successfully' : 'Verification failed'))
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      show('error', err.response?.data?.detail ?? 'Verification request failed')
    },
  })

  const connected = !!existingKey
  const anyLoading = storeMut.isPending || removeMut.isPending || verifyMut.isPending

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!keyId.trim() || !privKey.trim()) return
    storeMut.mutate()
  }

  return (
    <div className="card relative">
      <Toast toast={toast} />

      {/* Header ─────────────────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-surface-2 border border-border flex items-center justify-center shrink-0">
            <Key size={14} className="text-muted" />
          </div>
          <div>
            <h2 className="text-slate-100 font-semibold text-sm">{config.label}</h2>
            <div className="flex items-center gap-1.5 mt-0.5">
              <span
                className={[
                  'inline-block w-1.5 h-1.5 rounded-full',
                  connected ? 'bg-success' : 'bg-muted',
                ].join(' ')}
              />
              <span className="text-xs text-muted">
                {connected ? 'Connected' : 'Not connected'}
              </span>
            </div>
          </div>
        </div>
      </div>

      {/* Connected state ──────────────────────────────────────────────────── */}
      {connected && (
        <div className="space-y-3">
          <div className="flex gap-6 text-xs text-muted">
            <span>
              <span className="text-slate-400 font-medium">Created: </span>
              {fmtDate(existingKey.created_at)}
            </span>
            <span>
              <span className="text-slate-400 font-medium">Last used: </span>
              {fmtDate(existingKey.last_used_at)}
            </span>
          </div>

          <div className="flex items-center gap-2 pt-1">
            <button
              type="button"
              className="btn-ghost text-xs py-1.5"
              disabled={anyLoading}
              onClick={() => verifyMut.mutate()}
            >
              {verifyMut.isPending
                ? <Loader2 size={13} className="animate-spin" />
                : <Zap      size={13} />
              }
              Verify Connection
            </button>

            <button
              type="button"
              className="btn-danger text-xs py-1.5"
              disabled={anyLoading}
              onClick={() => removeMut.mutate()}
            >
              {removeMut.isPending
                ? <Loader2 size={13} className="animate-spin" />
                : <Trash2  size={13} />
              }
              Remove
            </button>
          </div>
        </div>
      )}

      {/* No-key state ─────────────────────────────────────────────────────── */}
      {!connected && (
        <div className="space-y-3">
          {!formOpen ? (
            <button
              type="button"
              className="btn-ghost text-xs py-1.5"
              onClick={() => setFormOpen(true)}
            >
              <Plus size={13} />
              Add credentials
            </button>
          ) : (
            <form onSubmit={handleSubmit} className="space-y-3 pt-1">
              {/* Key ID */}
              <div>
                <label className="label">{config.keyIdLabel}</label>
                <input
                  type="text"
                  className="input text-xs"
                  placeholder={config.keyIdPlaceholder}
                  value={keyId}
                  onChange={e => setKeyId(e.target.value)}
                  autoComplete="off"
                  autoFocus
                />
              </div>

              {/* Private Key */}
              <div>
                <label className="label">Private Key (PEM)</label>
                <textarea
                  className="input font-mono text-xs resize-none leading-relaxed"
                  rows={6}
                  placeholder={'-----BEGIN EC PRIVATE KEY-----\n…\n-----END EC PRIVATE KEY-----'}
                  value={privKey}
                  onChange={e => setPrivKey(e.target.value)}
                  autoComplete="off"
                  spellCheck={false}
                />
              </div>

              <div className="flex items-center gap-2">
                <button
                  type="submit"
                  className="btn-primary text-xs py-1.5"
                  disabled={anyLoading || !keyId.trim() || !privKey.trim()}
                >
                  {storeMut.isPending
                    ? <Loader2 size={13} className="animate-spin" />
                    : <CheckCircle size={13} />
                  }
                  Save credentials
                </button>
                <button
                  type="button"
                  className="btn-ghost text-xs py-1.5"
                  disabled={anyLoading}
                  onClick={() => { setFormOpen(false); setKeyId(''); setPrivKey('') }}
                >
                  Cancel
                </button>
              </div>
            </form>
          )}
        </div>
      )}
    </div>
  )
}

// ── Page ─────────────────────────────────────────────────────────────────────

export default function KeysPage() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['keys'],
    queryFn:  async () => {
      const res = await keys.list()
      return res.data as ApiKey[]
    },
  })

  const keysByExchange: Record<string, ApiKey> = {}
  if (data) {
    for (const k of data) keysByExchange[k.exchange] = k
  }

  return (
    <div className="max-w-xl space-y-6">
      {/* Page heading */}
      <div>
        <h1 className="text-xl font-semibold text-slate-100">API Keys</h1>
        <p className="text-sm text-muted mt-1">
          Manage exchange credentials used for order execution.
        </p>
      </div>

      {isError && (
        <div className="card border-danger/30 bg-danger/5 text-danger text-sm flex items-center gap-2">
          <XCircle size={14} />
          Failed to load API keys. Please refresh.
        </div>
      )}

      {isLoading ? (
        /* Skeleton */
        <div className="space-y-4">
          {[0, 1].map(i => (
            <div key={i} className="card animate-pulse space-y-3">
              <div className="flex items-center gap-3">
                <div className="w-8 h-8 rounded-lg bg-surface-3" />
                <div className="space-y-1.5">
                  <div className="h-3 w-20 rounded bg-surface-3" />
                  <div className="h-2.5 w-16 rounded bg-surface-3" />
                </div>
              </div>
              <div className="h-2.5 w-32 rounded bg-surface-3" />
            </div>
          ))}
        </div>
      ) : (
        <div className="space-y-4">
          {EXCHANGES.map(cfg => (
            <ExchangeCard
              key={cfg.id}
              config={cfg}
              existingKey={keysByExchange[cfg.id]}
            />
          ))}
        </div>
      )}
    </div>
  )
}
