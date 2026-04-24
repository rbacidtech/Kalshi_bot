import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useAuth } from '../lib/auth'
import { edgePulseWS, WSMessage } from '../lib/ws'

const ACTIVITY_MAX = 50   // keep last N events in cache

export function useEdgePulseWS() {
  const qc                            = useQueryClient()
  const { user }                      = useAuth()
  const [connected, setConnected]     = useState(false)

  useEffect(() => {
    if (!user) {
      edgePulseWS.disconnect()
      setConnected(false)
      return
    }

    edgePulseWS.connect(
      (msg: WSMessage) => {
        switch (msg.type) {
          case 'portfolio':
            if (msg.data) qc.setQueryData(['portfolio'], msg.data)
            break

          case 'status':
            if (msg.data) qc.setQueryData(['bot-status'], msg.data)
            break

          case 'activity': {
            const newEvents = (msg.data as unknown[]) ?? []
            if (!newEvents.length) break
            qc.setQueryData<{ events: unknown[] }>(['activity'], old => {
              const existing = old?.events ?? []
              // prepend new events, cap to ACTIVITY_MAX
              return { events: [...newEvents, ...existing].slice(0, ACTIVITY_MAX) }
            })
            break
          }

          case 'ping':
            // keepalive — no action needed
            break

          case 'error':
            console.warn('WS server error:', msg.detail)
            break
        }
      },
      (isConnected) => setConnected(isConnected),
    )

    return () => {
      // Don't disconnect on re-render — only when user logs out (handled above)
    }
  }, [user, qc])

  return { connected }
}
