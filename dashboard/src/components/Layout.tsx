import { Outlet, useLocation } from 'react-router-dom'
import Sidebar from './Sidebar'
import Topbar from './Topbar'

/** Map route pathnames to human-readable titles. */
const ROUTE_TITLES: Record<string, string> = {
  '/dashboard':    'Portfolio',
  '/keys':         'API Keys',
  '/subscription': 'Subscription',
  '/admin':        'Admin',
}

export default function Layout() {
  const { pathname } = useLocation()
  const pageTitle    = ROUTE_TITLES[pathname] ?? ''

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-surface-0">
      {/* Fixed-width left sidebar */}
      <Sidebar />

      {/* Right column: topbar + scrollable content */}
      <div className="flex flex-col flex-1 min-w-0 overflow-hidden">
        <Topbar title={pageTitle} />

        <main className="flex-1 overflow-y-auto px-6 py-6 animate-fadeIn">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
