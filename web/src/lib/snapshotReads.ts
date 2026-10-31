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
