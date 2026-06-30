"use client";

import { useEffect, useRef, useState } from "react";
import {
  FilePlus2,
  Save,
  Loader2,
  RefreshCw,
  Undo2,
  Redo2,
  Maximize,
  Upload,
  Download,
  Image as ImageIcon,
  LayoutGrid,
  ChevronDown,
  ArrowDown,
  ArrowRight,
  Share2,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Switch } from "@/components/ui/switch";
import {
  type ArrangeSettings,
  NODE_SEP_RANGE,
  RANK_SEP_RANGE,
  GROUP_SEP_RANGE,
} from "@/lib/metrics-canvas/arrange-settings";

export interface ToolbarProps {
  canUndo: boolean;
  canRedo: boolean;
  /** True when the map has unsaved changes (drives the Save button highlight). */
  dirty: boolean;
  /** True while a save request is in flight. */
  saving: boolean;
  /** Whether the map can be saved (there is something on the canvas). */
  canSave: boolean;
  /** Whether autosave is currently enabled. */
  autosave: boolean;
  /** Current auto-arrange settings (direction / spacing / stagger). */
  arrange: ArrangeSettings;
  onNew: () => void;
  onSave: () => void;
  onToggleAutosave: () => void;
  onUndo: () => void;
  onRedo: () => void;
  onArrange: () => void;
  onArrangeChange: (next: ArrangeSettings) => void;
  onFit: () => void;
  onImport: () => void;
  onExportJson: () => void;
  onExportPng: () => void;
  onShare: () => void;
}

function IconBtn({
  onClick,
  title,
  disabled,
  children,
}: {
  onClick: () => void;
  title: string;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={title}
      className="grid h-8 w-8 place-items-center rounded-md border border-line-strong bg-panel text-foreground transition-colors hover:bg-panel2 disabled:opacity-40"
    >
      {children}
    </button>
  );
}

/** Two/three mutually-exclusive pill buttons. */
function Seg<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T;
  options: { value: T; label: React.ReactNode; title?: string }[];
  onChange: (v: T) => void;
}) {
  return (
    <div className="flex gap-0.5 rounded-md border border-line bg-panel2/60 p-0.5">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          title={o.title}
          onClick={() => onChange(o.value)}
          className={cn(
            "flex flex-1 items-center justify-center gap-1 rounded px-2 py-1 text-[11.5px] font-medium transition-colors",
            value === o.value ? "bg-brand/15 text-brand" : "text-faint hover:text-foreground",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** A labelled distance slider showing its live px value. */
function DistanceSlider({
  label,
  value,
  min,
  max,
  step,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (v: number) => void;
}) {
  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <span className="text-[10.5px] font-semibold uppercase tracking-[0.04em] text-faint">{label}</span>
        <span className="text-[11px] tabular-nums text-foreground/70">{value}px</span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="h-1.5 w-full cursor-pointer appearance-none rounded-full bg-line accent-brand"
      />
    </div>
  );
}

/** Split button: the left half arranges with current settings; the caret opens a
 *  popover to tune direction, the two spacing distances, and stagger. */
function ArrangeControl({
  arrange,
  onArrange,
  onArrangeChange,
}: {
  arrange: ArrangeSettings;
  onArrange: () => void;
  onArrangeChange: (next: ArrangeSettings) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const patch = (p: Partial<ArrangeSettings>) => onArrangeChange({ ...arrange, ...p });

  // nodeSep spaces nodes ACROSS the flow; rankSep spaces successive steps ALONG
  // it. Which on-screen axis each maps to flips with direction — so label by the
  // axis the user actually sees. Otherwise "Node spacing" looks broken on
  // connected boxes (their gap is the along-flow one) and only nudges unconnected
  // siblings — exactly the "works only on some" confusion.
  const isVertical = arrange.direction === "vertical";
  const crossLabel = isVertical ? "Horizontal gap ↔" : "Vertical gap ↕";
  const flowLabel = isVertical ? "Vertical gap ↕" : "Horizontal gap ↔";

  return (
    <div ref={ref} className="relative flex items-center">
      <button
        onClick={onArrange}
        title="Tidy up — auto-arrange & route edges"
        className="grid h-8 w-8 place-items-center rounded-l-md border border-line-strong bg-panel text-foreground transition-colors hover:bg-panel2"
      >
        <LayoutGrid className="h-4 w-4" />
      </button>
      <button
        onClick={() => setOpen((o) => !o)}
        title="Auto-arrange settings"
        aria-label="Auto-arrange settings"
        className={cn(
          "grid h-8 w-5 place-items-center rounded-r-md border border-l-0 border-line-strong transition-colors",
          open ? "bg-brand/15 text-brand" : "bg-panel text-faint hover:bg-panel2",
        )}
      >
        <ChevronDown className="h-3.5 w-3.5" />
      </button>

      {open && (
        <div className="absolute left-0 top-9 z-30 w-60 rounded-md border border-line bg-panel p-3 shadow-card">
          <div className="mb-3">
            <div className="mb-1 text-[10.5px] font-semibold uppercase tracking-[0.04em] text-faint">Direction</div>
            <Seg
              value={arrange.direction}
              onChange={(v) => patch({ direction: v })}
              options={[
                {
                  value: "vertical",
                  title: "Top to bottom",
                  label: (
                    <>
                      <ArrowDown className="h-3 w-3" /> Vertical
                    </>
                  ),
                },
                {
                  value: "horizontal",
                  title: "Left to right",
                  label: (
                    <>
                      <ArrowRight className="h-3 w-3" /> Horizontal
                    </>
                  ),
                },
              ]}
            />
          </div>

          <div className="mb-3 space-y-3">
            <div className="space-y-2.5">
              <div className="text-[9.5px] font-semibold uppercase tracking-[0.06em] text-faint/60">
                Inside groups
              </div>
              <DistanceSlider
                label={crossLabel}
                value={arrange.nodeSep}
                min={NODE_SEP_RANGE.min}
                max={NODE_SEP_RANGE.max}
                step={NODE_SEP_RANGE.step}
                onChange={(v) => patch({ nodeSep: v })}
              />
              <DistanceSlider
                label={flowLabel}
                value={arrange.rankSep}
                min={RANK_SEP_RANGE.min}
                max={RANK_SEP_RANGE.max}
                step={RANK_SEP_RANGE.step}
                onChange={(v) => patch({ rankSep: v })}
              />
            </div>
            <div className="space-y-2.5">
              <div className="text-[9.5px] font-semibold uppercase tracking-[0.06em] text-faint/60">
                Between groups
              </div>
              <DistanceSlider
                label="Group gap"
                value={arrange.groupSep}
                min={GROUP_SEP_RANGE.min}
                max={GROUP_SEP_RANGE.max}
                step={GROUP_SEP_RANGE.step}
                onChange={(v) => patch({ groupSep: v })}
              />
            </div>
          </div>

          <label className="flex items-center justify-between gap-2">
            <span className="text-[12px] text-foreground">Stagger stacked nodes</span>
            <Switch checked={arrange.stagger} onCheckedChange={(v) => patch({ stagger: v })} />
          </label>

          <button
            onClick={() => {
              setOpen(false);
              onArrange();
            }}
            className="mt-3 w-full rounded-md bg-brand px-2 py-1.5 text-[12px] font-medium text-white transition-colors hover:bg-brand/90"
          >
            Apply &amp; arrange
          </button>
        </div>
      )}
    </div>
  );
}

export function Toolbar(p: ToolbarProps) {
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-line bg-panel/60 px-2.5 py-2">
      <IconBtn onClick={p.onNew} title="New map">
        <FilePlus2 className="h-4 w-4" />
      </IconBtn>

      {/* Save: highlighted + dotted while there are unsaved changes. */}
      <button
        onClick={p.onSave}
        disabled={!p.canSave || p.saving}
        title={p.saving ? "Saving…" : p.dirty ? "Save changes (Ctrl+S)" : "All changes saved"}
        className={cn(
          "relative grid h-8 w-8 place-items-center rounded-md border transition-colors disabled:opacity-40",
          p.dirty && !p.saving
            ? "border-brand bg-brand/10 text-brand hover:bg-brand/20"
            : "border-line-strong bg-panel text-foreground hover:bg-panel2",
        )}
      >
        {p.saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
        {p.dirty && !p.saving && (
          <span className="absolute -right-0.5 -top-0.5 h-2 w-2 rounded-full bg-brand ring-2 ring-panel" />
        )}
      </button>

      {/* Autosave toggle (off by default — manual save is the norm). */}
      <button
        onClick={p.onToggleAutosave}
        title={
          p.autosave
            ? "Autosave is ON — changes save automatically. Click to turn off."
            : "Autosave is OFF — save manually with Ctrl+S. Click to turn on."
        }
        className={cn(
          "flex h-8 items-center gap-1.5 rounded-md border px-2 text-[11px] font-medium transition-colors",
          p.autosave
            ? "border-brand bg-brand/10 text-brand hover:bg-brand/20"
            : "border-line-strong bg-panel text-faint hover:bg-panel2",
        )}
      >
        <RefreshCw className="h-3.5 w-3.5" />
        Autosave {p.autosave ? "on" : "off"}
      </button>

      <div className="mx-1 h-5 w-px bg-line" />

      <IconBtn onClick={p.onUndo} title="Undo (Ctrl+Z)" disabled={!p.canUndo}>
        <Undo2 className="h-4 w-4" />
      </IconBtn>
      <IconBtn onClick={p.onRedo} title="Redo (Ctrl+Y)" disabled={!p.canRedo}>
        <Redo2 className="h-4 w-4" />
      </IconBtn>
      <ArrangeControl arrange={p.arrange} onArrange={p.onArrange} onArrangeChange={p.onArrangeChange} />
      <IconBtn onClick={p.onFit} title="Fit view">
        <Maximize className="h-4 w-4" />
      </IconBtn>

      <div className="mx-1 h-5 w-px bg-line" />

      <IconBtn onClick={p.onImport} title="Import JSON">
        <Upload className="h-4 w-4" />
      </IconBtn>
      <IconBtn onClick={p.onExportJson} title="Export JSON">
        <Download className="h-4 w-4" />
      </IconBtn>
      <IconBtn onClick={p.onExportPng} title="Export PNG">
        <ImageIcon className="h-4 w-4" />
      </IconBtn>

      <div className="mx-1 h-5 w-px bg-line" />

      <IconBtn onClick={p.onShare} title="Share map">
        <Share2 className="h-4 w-4" />
      </IconBtn>
    </div>
  );
}
