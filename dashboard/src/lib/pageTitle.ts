import { create } from 'zustand'

interface PageTitleState {
  title: string
  setTitle: (title: string) => void
}

const usePageTitleStore = create<PageTitleState>()((set) => ({
  title:    '',
  setTitle: (title) => set({ title }),
}))

/** Read the current page title (used by Topbar). */
export function usePageTitle(): string {
  return usePageTitleStore((s) => s.title)
}

/** Set the page title from a page component. */
export function useSetPageTitle(title: string): void {
  // Zustand store setter — call once on mount via useEffect in pages,
  // or simply call directly inside a useEffect.
  const setTitle = usePageTitleStore((s) => s.setTitle)
  // Called as a hook: runs during render to keep title in sync.
  // Safe because it's a synchronous Zustand set (no batching issues).
  setTitle(title)
}
