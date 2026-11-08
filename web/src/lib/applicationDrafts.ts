import { createStore } from "zustand/vanilla";
import { applications } from "./applicationList";
import { textDrafts, type TextDraft } from "./textDrafts";

const records = new WeakMap<TextDraft, ApplicationDraft>();

/** App actions retain their own busy/error state after the pane leaves. */
class ApplicationDraft {
  readonly store = createStore(() => ({ busy: false, error: null as string | null }));
  readonly text: TextDraft;
  private readonly id: string;
  constructor(id: string, text: TextDraft) { this.id = id; this.text = text; }

  private async perform(call: () => Promise<unknown>, fallback: string): Promise<boolean> {
    if (!this.text.current() || this.store.getState().busy) return false;
    this.store.setState({ busy: true, error: null });
    try {
      await call();
      return this.text.current();
    } catch (error) {
      if (this.text.current()) this.store.setState({ error: error instanceof Error ? error.message : fallback });
      return false;
    } finally {
      if (this.text.current()) this.store.setState({ busy: false });
    }
  }

  async build(): Promise<boolean> {
    const { text, version } = this.text.store.getState();
    if (!text.trim()) return false;
    const accepted = await this.perform(() => {
      this.text.consume(version);
      return applications.build(this.id, text.trim());
    }, "Failed to start the build");
    return accepted && this.text.clear(version);
  }
  archive() { return this.perform(() => applications.archive(this.id), "Failed to archive"); }
  remove(keepFiles: boolean) { return this.perform(() => applications.remove(this.id, keepFiles), "Deletion is unfinished"); }
}

export function applicationDraft(id: string) {
  const text = textDrafts.get("application", id);
  let record = records.get(text);
  if (!record) { record = new ApplicationDraft(id, text); records.set(text, record); }
  return record;
}

applications.onDeleted(id => textDrafts.remove("application", id));
