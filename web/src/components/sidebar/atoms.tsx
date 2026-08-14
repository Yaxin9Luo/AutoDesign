import type { ReactNode } from "react";
import { translate } from "@/lib/i18n";
import { useApp } from "@/lib/store";

export function PanelSection({
  title,
  right,
  children,
}: {
  title: string;
  right?: ReactNode;
  children: ReactNode;
}) {
  const language = useApp((s) => s.ui_language);
  return (
    <section className="border-b border-ink-300/50 px-4 py-4">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="eyebrow-rule">{translate(language, title)}</h3>
        {right}
      </div>
      <div className="space-y-3">{children}</div>
    </section>
  );
}

export function Field({
  label,
  children,
  inline,
}: {
  label: string;
  children: ReactNode;
  inline?: boolean;
}) {
  const language = useApp((s) => s.ui_language);
  const labelText = translate(language, label);
  if (inline) {
    return (
      <label className="flex items-center justify-between gap-3">
        <span className="field-label">{labelText}</span>
        <div className="flex-1">{children}</div>
      </label>
    );
  }
  return (
    <label className="block">
      <span className="field-label">{labelText}</span>
      <div className="mt-1.5">{children}</div>
    </label>
  );
}

export function NumberField({
  value,
  onChange,
  min,
  max,
  step = 1,
  suffix,
}: {
  value: number | undefined;
  onChange: (v: number) => void;
  min?: number;
  max?: number;
  step?: number;
  suffix?: string;
}) {
  return (
    <div className="relative">
      <input
        type="number"
        value={value ?? ""}
        min={min}
        max={max}
        step={step}
        onChange={(e) => onChange(parseFloat(e.target.value || "0"))}
        className="field-input tabular pr-7"
      />
      {suffix && (
        <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-[11px] text-ink-500">
          {suffix}
        </span>
      )}
    </div>
  );
}

export function ColorField({
  value,
  onChange,
}: {
  value: string | undefined;
  onChange: (v: string) => void;
}) {
  const v = value ?? "#000000";
  const pickerValue = /^#[0-9a-f]{6}$/i.test(v) ? v : "#000000";
  const recent = useApp((s) => s.recent_colors);
  const rememberColor = useApp((s) => s.rememberColor);
  const commit = (color: string) => {
    onChange(color);
    rememberColor(color);
  };
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-2 rounded-md border border-ink-300/75 bg-surface-raised/80 px-2 py-1.5 transition focus-within:border-accent">
        <label
          className="block h-5 w-5 cursor-pointer rounded border border-ink-300/70 shadow-[inset_0_0_0_1px_rgba(255,255,255,0.6)]"
          style={{ background: v }}
        >
          <input
            type="color"
            value={pickerValue}
            onChange={(e) => commit(e.target.value)}
            className="h-0 w-0 opacity-0"
          />
        </label>
        <input
          type="text"
          value={v}
          onChange={(e) => commit(e.target.value)}
          className="tabular w-full bg-transparent text-[12px] text-ink-900 outline-none"
        />
      </div>
      {recent.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {recent.map((c) => (
            <button
              key={c}
              type="button"
              title={c}
              onClick={() => commit(c)}
              className="h-4 w-4 rounded border border-ink-300/70 shadow-[inset_0_0_0_1px_rgba(255,255,255,0.45)]"
              style={{ background: c }}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export function SelectField({
  value,
  onChange,
  options,
}: {
  value: string | undefined;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
}) {
  const language = useApp((s) => s.ui_language);
  return (
    <select
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value)}
      className="field-input"
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {translate(language, o.label)}
        </option>
      ))}
    </select>
  );
}

export function SegGroup({
  value,
  onChange,
  options,
}: {
  value: string | undefined;
  onChange: (v: string) => void;
  options: { value: string; icon?: ReactNode; label?: string }[];
}) {
  const language = useApp((s) => s.ui_language);
  return (
    <div className="flex gap-1">
      {options.map((o) => (
        <button
          key={o.value}
          onClick={() => onChange(o.value)}
          className={`flex h-8 flex-1 items-center justify-center rounded-md text-ink-500 transition hover:bg-surface-raised hover:text-ink-900 ${
            value === o.value ? "bg-accent-soft text-accent-deep hover:bg-accent-soft hover:text-accent-deep" : ""
          }`}
          title={o.label ? translate(language, o.label) : undefined}
        >
          {o.icon ?? <span className="text-xs">{o.label ? translate(language, o.label) : ""}</span>}
        </button>
      ))}
    </div>
  );
}

export function SliderField({
  value,
  onChange,
  min,
  max,
  step = 1,
}: {
  value: number | undefined;
  onChange: (v: number) => void;
  min: number;
  max: number;
  step?: number;
}) {
  return (
    <input
      type="range"
      value={value ?? min}
      min={min}
      max={max}
      step={step}
      onChange={(e) => onChange(parseFloat(e.target.value))}
      className="w-full accent-accent"
    />
  );
}
