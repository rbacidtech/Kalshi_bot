// Handle Microsoft OAuth token before React mounts — avoids auth race condition
const _msToken = new URLSearchParams(window.location.search).get('ms_token')
if (_msToken) {
  localStorage.setItem('ep_access', _msToken)
  window.location.replace('/dashboard')
}

import React from 'react'
import ReactDOM from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import './index.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      staleTime: 30_000,
      refetchInterval: 30_000,
    },
  },
})

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>
)
