import { useState, useEffect } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import Sidebar from './Sidebar'
import Topbar from './Topbar'
import { Toaster } from './Toast'

/** Map route pathnames to human-readable titles. */
const ROUTE_TITLES: Record<string, string> = {
  '/dashboard':    'Portfolio',
  '/performance':  'Performance',
  '/keys':         'API Keys',
  '/subscription': 'Subscription',
  '/controls':     'Bot Controls',
  '/admin':        'Admin',
}

function useIsMobile() {
  const [isMobile, setIsMobile] = useState(() => window.innerWidth < 768)
  useEffect(() => {
    const fn = () => setIsMobile(window.innerWidth < 768)
    window.addEventListener('resize', fn)
    return () => window.removeEventListener('resize', fn)
  }, [])
  return isMobile
}

export default function Layout() {
  const { pathname }                    = useLocation()
  const pageTitle                       = ROUTE_TITLES[pathname] ?? ''
  const isMobile                        = useIsMobile()
  const [sidebarOpen, setSidebarOpen]   = useState(false)

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-surface-0">

      {/* Desktop: push sidebar (hidden on mobile) */}
      <div className={`hidden md:block transition-all duration-200 shrink-0 overflow-hidden ${sidebarOpen ? 'w-60' : 'w-0'}`}>
        <Sidebar onClose={() => setSidebarOpen(false)} />
      </div>

      {/* Mobile: overlay sidebar + backdrop */}
      {isMobile && sidebarOpen && (
        <>
          <div
            className="fixed inset-0 bg-black/60 z-40 md:hidden"
            onClick={() => setSidebarOpen(false)}
          />
          <div className="fixed inset-y-0 left-0 z-50 w-72 md:hidden">
            <Sidebar onClose={() => setSidebarOpen(false)} />
          </div>
        </>
      )}

      {/* Main content */}
      <div className="flex flex-col flex-1 min-w-0 overflow-hidden">
        <Topbar
          title={pageTitle}
          sidebarOpen={sidebarOpen}
          onToggleSidebar={() => setSidebarOpen(o => !o)}
        />
        <main className="flex-1 overflow-y-auto px-4 py-4 md:px-6 md:py-6 animate-fadeIn">
          <Outlet />
        </main>
      </div>

      <Toaster />
    </div>
  )
}
