/** Preserve only entities changed by live events after this fetch began. */
export function mergeCardSnapshot<T>(snapshot: T[], current: T[], baseline: T[] | undefined, key: (item: T) => string): T[] {
  if (baseline === undefined) return snapshot;
  const start = new Map(baseline.map((item) => [key(item), item]));
  const changed = current.filter((item) => start.get(key(item)) !== item);
  return [...new Map([...snapshot, ...changed].map((item) => [key(item), item])).values()];
}

/** Auxiliary jobs have one execution per id. Only an event/receipt advances
 * provenance; another list response must not pretend to be a live update. */
export function mergeAuxiliarySnapshot<T extends { id: string; status: string }>(
  snapshot: T[], current: T[], baseline: T[] | undefined, updates: WeakMap<T, symbol>, advanceOnly = false,
): T[] {
  if (baseline === undefined) return snapshot;
  const start = new Map(baseline.map((item) => [item.id, item]));
  const live = new Map(current.map((item) => [item.id, item]));
  const active = (item: T) => item.status === "running" || item.status === "pending";
  const changed = (item: T) => {
    const before = start.get(item.id);
    return updates.get(item) !== (before ? updates.get(before) : undefined);
  };
  const result = new Map(snapshot.map((item) => {
    const previous = live.get(item.id);
    if (previous && (changed(previous) || advanceOnly && (previous.status === item.status || !active(previous)) ||
        !active(previous) && previous.status !== item.status ||
        previous.status === "running" && item.status === "pending")) return [item.id, previous];
    const adopted = { ...item };
    const marker = previous && updates.get(previous);
    if (marker) updates.set(adopted, marker);
    return [item.id, adopted];
  }));
  for (const item of current) {
    const before = start.get(item.id);
    if (!result.has(item.id) && (!before || changed(item) || active(before) && !active(item))) result.set(item.id, item);
  }
  return [...result.values()];
}
