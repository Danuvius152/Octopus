import type { ModelSettings } from "../api";
import { Label } from "./ui/label";

export function ServiceTierSelect({
  id, value, onChange, model, catalog, inherited, disabled,
}: {
  id: string;
  value: string;
  onChange: (value: string) => void;
  model?: string | null;
  catalog: ModelSettings | null;
  inherited?: string;
  disabled?: boolean;
}) {
  const selected = catalog?.models?.find((m) => m.id === (model || catalog.default_model));
  const options = selected?.service_tiers ?? catalog?.service_tiers ?? [];
  const fastAvailable = options.includes("fast");
  return (
    <div className="service-tier-field space-y-2">
      <Label htmlFor={id}>Speed</Label>
      <select
        id={id} value={value} onChange={(e) => onChange(e.target.value)}
        disabled={disabled || !catalog}
        className="service-tier-select h-9 w-full rounded-lg border border-border bg-card px-3 text-sm"
      >
        {inherited && <option value="">{inherited}</option>}
        <option value="default">Normal{!inherited ? " (default)" : ""}</option>
        <option value="fast" disabled={!fastAvailable}>
          Fast{!fastAvailable ? " — unavailable for this model" : ""}
        </option>
      </select>
      <p className="text-xs text-muted-foreground">Fast increases usage. Changes apply from the next turn.</p>
    </div>
  );
}
