/**
 * Singleton WebSocket client for EdgePulse real-time push.
 * Connects to /ws?token=<jwt>, auto-reconnects with exponential backoff.
 */

const BASE_URL = (import.meta.env.VITE_API_URL ?? window.location.origin)
  .replace(/^http/, 'ws')

export type WSMessageType = 'portfolio' | 'status' | 'activity' | 'ping' | 'error'

export interface WSMessage {
  type: WSMessageType
  data?: unknown
  ts?:   number
  detail?: string
}

export type WSHandler = (msg: WSMessage) => void
export type WSStatusHandler = (connected: boolean) => void

const BACKOFF_MS   = [1_000, 2_000, 4_000, 8_000, 15_000, 30_000]
const MAX_BACKOFF  = 30_000

class EdgePulseWS {
  private ws:           WebSocket | null = null
  private handler:      WSHandler | null = null
  private statusCb:     WSStatusHandler | null = null
  private attempt       = 0
  private destroyed     = false
  private retryTimer:   ReturnType<typeof setTimeout> | null = null

  connect(handler: WSHandler, onStatus: WSStatusHandler): void {
    this.handler   = handler
    this.statusCb  = onStatus
    this.destroyed = false
    this._open()
  }

  disconnect(): void {
    this.destroyed = true
    if (this.retryTimer) clearTimeout(this.retryTimer)
    this.ws?.close(1000, 'client disconnect')
    this.ws = null
  }

  private _open(): void {
    if (this.destroyed) return
    const token = localStorage.getItem('ep_access')
    if (!token) {
      // No token — wait and retry; login will trigger a reconnect
      this._scheduleRetry()
      return
    }

    const url = `${BASE_URL}/ws?token=${encodeURIComponent(token)}`
    const ws  = new WebSocket(url)
    this.ws   = ws

    ws.onopen = () => {
      this.attempt = 0
      this.statusCb?.(true)
    }

    ws.onmessage = (ev) => {
      try {
        const msg: WSMessage = JSON.parse(ev.data)
        this.handler?.(msg)
      } catch { /* ignore malformed */ }
    }

    ws.onclose = (ev) => {
      this.statusCb?.(false)
      // 4001/4003 = auth error — don't retry immediately
      if (ev.code === 4001 || ev.code === 4003) {
        this._scheduleRetry(10_000)
      } else if (!this.destroyed) {
        this._scheduleRetry()
      }
    }

    ws.onerror = () => {
      // onclose fires after onerror, handles retry
    }
  }

  private _scheduleRetry(fixedMs?: number): void {
    if (this.destroyed) return
    const delay = fixedMs ?? BACKOFF_MS[Math.min(this.attempt, BACKOFF_MS.length - 1)] ?? MAX_BACKOFF
    this.attempt++
    this.retryTimer = setTimeout(() => this._open(), delay)
  }
}

export const edgePulseWS = new EdgePulseWS()
