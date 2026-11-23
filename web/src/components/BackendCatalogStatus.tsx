import { loadBackendCatalog } from "../hooks/useBackendCatalog";
import type { BackendCatalogRead } from "../stores/sessionStore";

export function BackendCatalogStatus({ read }: { read: BackendCatalogRead }) {
  if (read.status === "ready") {
    return read.catalog.available.length === 0
      ? <p role="status" className="text-xs text-muted-foreground">No engine CLIs detected on this host.</p>
      : null;
  }
  if (read.status === "failed") {
    return (
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <p role="alert" className="text-destructive">Could not load engine catalog.</p>
        <button type="button" className="text-primary underline" onClick={() => void loadBackendCatalog()}>
          Retry engine catalog
        </button>
      </div>
    );
  }
  return <p role="status" className="text-xs text-muted-foreground">Loading engine catalog…</p>;
}
