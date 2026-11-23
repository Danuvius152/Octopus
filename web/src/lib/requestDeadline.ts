/** Bound a complete observation, including body decoding. The caller chooses
 * whether to pass the signal to its transport or only stop waiting. */
export class RequestDeadline {
  private controller = new AbortController();
  private until = Date.now() + 30_000;
  private timer = setTimeout(() => this.finish(), 30_000);
  get signal(): AbortSignal { return this.controller.signal; }
  get active(): boolean { return !this.signal.aborted && Date.now() < this.until; }
  finish(): void { clearTimeout(this.timer); this.controller.abort(); }
  wait<T>(work: Promise<T>): Promise<T | null> {
    return new Promise((resolve, reject) => {
      const abort = () => resolve(null);
      if (!this.active) { work.catch(() => {}); resolve(null); return; }
      this.signal.addEventListener("abort", abort, { once: true });
      work.then((result) => {
        this.signal.removeEventListener("abort", abort);
        resolve(this.active ? result : null);
      }, (error: unknown) => {
        this.signal.removeEventListener("abort", abort);
        if (this.active) reject(error);
        else resolve(null);
      });
    });
  }
}
