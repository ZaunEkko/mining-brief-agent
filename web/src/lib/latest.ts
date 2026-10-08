/**
 * Guards against stale async results: only the most recently issued request may
 * write to the UI. Anything issued before the last `next()` / `invalidate()` is stale.
 */
export function createLatest() {
  let current = 0
  return {
    next(): number {
      current += 1
      return current
    },
    invalidate(): void {
      current += 1
    },
    isCurrent(token: number): boolean {
      return token === current
    },
  }
}
