import type { ModelSettings } from "../api";
import { modelOption } from "../hooks/useModelSettings";
import { Label } from "./ui/label";

export function ServiceTierSelect({
  id, value, onChange, model, catalog, inherited, disabled, allowClear,
}: {
  id: string;
  value: string;
  onChange: (value: string) => void;
  model?: string | null;
  catalog: ModelSettings | null;
  inherited: string;
  disabled?: boolean;
  allowClear?: boolean;
}) {
  const selected = modelOption(catalog, model);
  const options = selected?.service_tiers ?? catalog?.service_tiers ?? [];
  const fastAvailable = options.includes("fast");
  return (
    <div className="service-tier-field space-y-2">
      <Label htmlFor={id}>Speed</Label>
      <select
        id={id} value={value} onChange={(e) => onChange(e.target.value)}
        disabled={disabled || (!catalog && !(allowClear && value))}
        className="service-tier-select h-9 w-full rounded-lg border border-border bg-card px-3 text-sm"
      >
        <option value="">{inherited}</option>
        <option value="default" disabled={!options.includes("default")}>
          Normal{catalog && !options.includes("default") && " — unavailable for this model"}
        </option>
        <option value="fast" disabled={!fastAvailable}>
          Fast{catalog && !fastAvailable && " — unavailable for this model"}
        </option>
      </select>
      {fastAvailable && <p className="text-xs text-muted-foreground">Fast increases usage.</p>}
    </div>
  );
}
