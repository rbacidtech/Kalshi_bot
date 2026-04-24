# EdgePulse Dashboard — Frontend

React SPA that surfaces live trading state from the EdgePulse system. Built with Vite + TypeScript + Tailwind + Recharts. Served in production as static files by the FastAPI backend at port 8502.

---

## Stack

| Layer | Library | Purpose |
|-------|---------|---------|
| Framework | React 18 + TypeScript | Component tree |
| Build | Vite | Dev server, prod bundler |
| Styles | Tailwind CSS v3 | Utility-first CSS |
| Data fetching | TanStack Query (React Query) | Server state, caching, polling |
| Auth state | Zustand | JWT token store |
| Charts | Recharts | P&L curves, distribution bars, equity chart |
| Routing | React Router v6 | Client-side navigation |

---

## Local Development

```bash
cd /root/EdgePulse/dashboard
npm install
npm run dev          # Vite dev server on :5173
```

The Vite config proxies `/api/*` → `http://localhost:8502` so the dev server talks to the live FastAPI backend. Make sure `edgepulse-api.service` is running.

### Environment variables (dev only)

Create `.env.local` if you need to override the API base URL:

```
VITE_API_URL=http://localhost:8502
```

In production this is not needed — the SPA is served from the same origin as the API.

---

## Production Build & Deploy

```bash
cd /root/EdgePulse/dashboard
npm run build        # outputs to dist/
```

FastAPI serves `dist/` as static files — no API restart needed after a frontend-only change. For API changes: `systemctl restart edgepulse-api.service`.

---

## Pages

| Route | File | Purpose |
|-------|------|---------|
| `/` | `DashboardPage.tsx` | Live positions table, P&L sparkline, balances, drawdown meter, activity feed |
| `/controls` | `ControlsPage.tsx` | Strategy toggles, risk sliders, bot status, halt/resume, AI suggest |
| `/performance` | `PerformancePage.tsx` | Equity curve, P&L distribution, strategy breakdown, win rate |
| `/advisor` | `AdvisorPage.tsx` | LLM advisor summary, strategy health grid, alert feed |
| `/admin` | `AdminPage.tsx` | User CRUD, platform stats (admin only) |
| `/keys` | `KeysPage.tsx` | Kalshi + Coinbase API key vault |
| `/notifications` | `NotificationsPage.tsx` | Fill / exit / circuit-breaker alert feed |
| `/subscriptions` | `SubscriptionPage.tsx` | Tier info, usage meter |
| `/login` | `LoginPage.tsx` | JWT auth |
| `/register` | `RegisterPage.tsx` | New account |

---

## API Calls at a Glance

All requests go to `/api/*` (proxied in dev, same-origin in prod). Auth header: `Authorization: Bearer <jwt>`.

| Page | Endpoints called |
|------|-----------------|
| Dashboard | `GET /positions`, `/positions/coinbase`, `/performance/history`, `/controls/status`, `/controls/config`, `/controls/activity` |
| Controls | `GET /controls/config`, `PATCH /controls/config`, `GET /controls/status`, `POST /controls/halt`, `POST /controls/resume`, `POST /controls/ai-suggest` |
| Performance | `GET /performance`, `/performance/equity-curve` |
| Advisor | `GET /advisor/status`, `/advisor/alerts` |
| Admin | `GET /admin/stats`, `/admin/users`, `PATCH /admin/users/{id}`, `DELETE /admin/users/{id}` |
| Keys | `GET /keys`, `POST /keys`, `DELETE /keys/{exchange}`, `GET /keys/{exchange}/verify` |
| Notifications | `GET /notifications` |
| Subscriptions | `GET /subscriptions/me`, `/subscriptions/tiers` |

Polling cadence: most pages refetch every 30 seconds via React Query `refetchInterval`.

---

## Charts

All charts use Recharts. Data is in cents; display layer converts to dollars with `/ 100`.

| Chart | Component | Data key | Status |
|-------|-----------|----------|--------|
| 24h P&L sparkline | `AreaChart` | `GET /performance/history` | Live |
| Position exposure | `BarChart` | Portfolio positions | Live |
| Equity curve | `AreaChart` | `GET /performance/equity-curve` | Live |
| P&L distribution | `BarChart` | `GET /performance` `.by_distribution` | Live |
| Win rate ring | SVG circle | `GET /performance` `.win_rate` | Live |
| Daily drawdown meter | Linear progress | `GET /controls/status` `.session_pnl` | Live |

---

## Auth Flow

1. `POST /auth/login` returns `{ access_token, refresh_token }`.
2. Tokens stored in Zustand (in-memory) + `localStorage` (refresh token only).
3. React Query `queryClient` has a global error handler — on 401 it clears Zustand and redirects to `/login`.
4. Refresh: on 401 response, the axios interceptor calls `POST /auth/refresh` once, then retries the original request.

---

## Known Issues

| Issue | Severity | Notes |
|-------|----------|-------|
| No WebSocket — 30s polling | Low | Acceptable for position-level data |
| Strategy toggles require service restart | Medium | UI warns; no in-app restart button |
| Coinbase key verify always fails | Medium | Backend stub at `keys.py:413` |
| Node status dict overwrite bug | High | `controls.py:259` — intel node data can be silently dropped |

---

## Type Conventions

- All monetary values in the API responses are in **cents** (`_cents` suffix).
- Timestamps are ISO-8601 strings (`2026-04-24T12:00:00Z`).
- Confidence values are floats in `[0, 1]`.
- Edge values are floats in `[0, 1]` (0.10 = 10¢ edge on a $1 contract).
