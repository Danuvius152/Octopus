import type { ModelSettings } from "../api";
import { modelOption } from "../hooks/useModelSettings";
import { Label } from "./ui/label";

const descriptions: Record<string, string> = {
  low: "Low — faster responses",
  medium: "Medium — balanced",
  high: "High — deeper reasoning",
  xhigh: "Extra high",
  max: "Maximum",
  ultra: "Ultra — includes automatic delegation",
  minimal: "Minimal",
  none: "None",
};

export function ReasoningEffortSelect({
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
  const options = selected?.reasoning_efforts ?? catalog?.reasoning_efforts ?? [];
  return (
    <div className="reasoning-effort-field space-y-2">
      <Label htmlFor={id}>Reasoning effort</Label>
      <select
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled || (!catalog && !(allowClear && value))}
        className="reasoning-effort-select h-9 w-full rounded-lg border border-border bg-card px-3 text-sm"
      >
        <option value="">{inherited}</option>
        {value && !options.includes(value) && <option value={value} disabled={!!catalog}>
          {catalog ? `${value} — unavailable for this model` : descriptions[value] ?? value}
        </option>}
        {options.map((effort) => <option key={effort} value={effort}>{descriptions[effort] ?? effort}</option>)}
      </select>
    </div>
  );
}
