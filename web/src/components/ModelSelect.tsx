import type { ModelSettings } from "../api";
import { Label } from "./ui/label";

export function ModelSelect({
  id, value, onChange, catalog, inherited, disabled, allowClear,
}: {
  id: string;
  value: string;
  onChange: (value: string) => void;
  catalog: ModelSettings | null;
  inherited: string;
  disabled?: boolean;
  allowClear?: boolean;
}) {
  const models = catalog?.models ?? [];
  const outsideCatalog = value && !models.some((model) => model.id === value);
  return (
    <div className="space-y-2">
      <Label htmlFor={id}>Model</Label>
      <select
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled || (!catalog && !(allowClear && value))}
        className="h-9 w-full rounded-lg border border-border bg-card px-3 text-sm"
      >
        <option value="">{inherited}</option>
        {models.map((model) => <option key={model.id} value={model.id}>{model.name}</option>)}
        {outsideCatalog && <option value={value}>{value}{catalog && " · outside catalog"}</option>}
      </select>
    </div>
  );
}
