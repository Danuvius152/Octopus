/** Preserve only entities changed by live events after this fetch began. */
export function mergeCardSnapshot<T>(snapshot: T[], current: T[], baseline: T[] | undefined, key: (item: T) => string): T[] {
  if (baseline === undefined) return snapshot;
  const start = new Map(baseline.map((item) => [key(item), item]));
  const changed = current.filter((item) => start.get(key(item)) !== item);
  return [...new Map([...snapshot, ...changed].map((item) => [key(item), item])).values()];
}
