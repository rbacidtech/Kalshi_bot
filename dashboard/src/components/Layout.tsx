import { useState } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import Sidebar from './Sidebar'
import Topbar from './Topbar'

/** Map route pathnames to human-readable titles. */
const ROUTE_TITLES: Record<string, string> = {
  '/dashboard':    'Portfolio',
  '/performance':  'Performance',
  '/keys':         'API Keys',
  '/subscription': 'Subscription',
  '/controls':     'Bot Controls',
  '/admin':        'Admin',
}

export default function Layout() {
  const { pathname }          = useLocation()
  const pageTitle             = ROUTE_TITLES[pathname] ?? ''
  const [sidebarOpen, setSidebarOpen] = useState(true)

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-surface-0">
      {/* Collapsible left sidebar */}
      <div
        className={`transition-all duration-200 ease-in-out shrink-0 overflow-hidden ${
          sidebarOpen ? 'w-60' : 'w-0'
        }`}
      >
        <Sidebar onClose={() => setSidebarOpen(false)} />
      </div>

      {/* Right column: topbar + scrollable content */}
      <div className="flex flex-col flex-1 min-w-0 overflow-hidden">
        <Topbar
          title={pageTitle}
          sidebarOpen={sidebarOpen}
          onToggleSidebar={() => setSidebarOpen(o => !o)}
        />

        <main className="flex-1 overflow-y-auto px-6 py-6 animate-fadeIn">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
