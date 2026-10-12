import type { ModelSettings } from "../api";
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
  id, value, onChange, model, catalog, inherited, disabled,
}: {
  id: string;
  value: string;
  onChange: (value: string) => void;
  model?: string | null;
  catalog: ModelSettings | null;
  inherited: string;
  disabled?: boolean;
}) {
  const selected = catalog?.models?.find((m) => m.id === (model || catalog.default_model));
  const options = selected?.reasoning_efforts ?? catalog?.reasoning_efforts ?? [];
  return (
    <div className="reasoning-effort-field space-y-2">
      <Label htmlFor={id}>Reasoning effort</Label>
      <select
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled || !catalog}
        className="reasoning-effort-select h-9 w-full rounded-lg border border-border bg-card px-3 text-sm"
      >
        <option value="">{inherited}</option>
        {value && !options.includes(value) && <option value={value}>{value} — unavailable for this model</option>}
        {options.map((effort) => <option key={effort} value={effort}>{descriptions[effort] ?? effort}</option>)}
      </select>
      <p className="text-xs text-muted-foreground">Higher effort can take longer. Changing effort keeps the conversation history.</p>
    </div>
  );
}
