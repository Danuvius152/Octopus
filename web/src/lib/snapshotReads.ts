/** Order outstanding reads without retaining any entity or server revision. */
export class SnapshotReads {
  private pending = new Map<string, Set<symbol>>();

  begin(key: string) {
    const reads = this.pending.get(key) ?? new Set<symbol>();
    const read = Symbol();
    reads.add(read); this.pending.set(key, reads);
    return {
      claim: () => {
        if (!reads.has(read)) return false;
        for (const older of reads) { reads.delete(older); if (older === read) break; }
        return true;
      },
      finish: () => {
        reads.delete(read);
        if (!reads.size && this.pending.get(key) === reads) this.pending.delete(key);
      },
    };
  }
}

/** Order claims per task. A settled claim remains relevant only while an older
 * overlapping read can return. */
export class TaskSnapshotReads {
  private groups = new Map<string, { next: number; pending: Set<number>; claims: Map<string, number> }>();

  begin(key: string) {
    const group = this.groups.get(key) ?? { next: 0, pending: new Set<number>(), claims: new Map<string, number>() };
    this.groups.set(key, group);
    const order = ++group.next;
    group.pending.add(order);
    return {
      claim: (id: string) => {
        if (!group.pending.has(order) || (group.claims.get(id) ?? 0) > order) return false;
        group.claims.set(id, order);
        return true;
      },
      finish: () => {
        group.pending.delete(order);
        if (!group.pending.size && this.groups.get(key) === group) this.groups.delete(key);
      },
    };
  }
}
