"use client";

import { useEffect, useState } from "react";
import { useReactFlow } from "@xyflow/react";
import {
  Undo2,
  Redo2,
  Maximize,
  LayoutGrid,
  ChevronsDownUp,
  ChevronsUpDown,
  Eye,
  ChevronDown,
  Layers as LayersIcon,
  Tag,
} from "lucide-react";
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from "@/components/ui/select";
import { LENS_ORDER, LENSES, LAYER_LABEL, type LensId, type CardLayer } from "@/lib/lineage/lens";

export interface BottomToolbarProps {
  canUndo: boolean;
  canRedo: boolean;
  onUndo: () => void;
  onRedo: () => void;
  onArrange: () => void;
  onCollapseAll: () => void;
  onExpandAll: () => void;
  hasHidden: boolean;
  onUnhideAll: () => void;
  lens: LensId;
  onLensChange: (lens: LensId) => void;
  layers: CardLayer[];
  hiddenLayers: Set<string>;
  onToggleLayer: (layer: string) => void;
  tags: string[];
  tagsFilter: Set<string>;
  onToggleTag: (tag: string) => void;
}

/** colibri-style bottom toolbar: undo/redo · layers · tags · fit · arrange · lens. */
export function BottomToolbar(props: BottomToolbarProps) {
  const { fitView } = useReactFlow();
  return (
    <div className="flex items-center gap-1 rounded-lg border border-line bg-panel/95 px-1.5 py-1 shadow-card backdrop-blur">
      <ToolButton title="Undo (Ctrl+Z)" disabled={!props.canUndo} onClick={props.onUndo}>
        <Undo2 className="h-4 w-4" />
      </ToolButton>
      <ToolButton title="Redo (Ctrl+Shift+Z)" disabled={!props.canRedo} onClick={props.onRedo}>
        <Redo2 className="h-4 w-4" />
      </ToolButton>
      <Divider />

      <FilterDropdown
        icon={<LayersIcon className="h-3.5 w-3.5" />}
        label="layers"
        activeCount={props.hiddenLayers.size}
        items={props.layers.map((l) => ({ key: l, label: LAYER_LABEL[l] }))}
        isChecked={(k) => !props.hiddenLayers.has(k)}
        onToggle={props.onToggleLayer}
        emptyHint="No layers"
      />
      <FilterDropdown
        icon={<Tag className="h-3.5 w-3.5" />}
        label="tags"
        activeCount={props.tagsFilter.size}
        items={props.tags.map((t) => ({ key: t, label: t }))}
        isChecked={(k) => props.tagsFilter.has(k)}
        onToggle={props.onToggleTag}
        emptyHint="No tags on these models"
      />
      <Divider />

      <ToolButton title="Fit to view" onClick={() => fitView({ padding: 0.2, duration: 300 })}>
        <Maximize className="h-4 w-4" />
      </ToolButton>
      <ToolButton title="Auto-arrange" onClick={props.onArrange}>
        <LayoutGrid className="h-4 w-4" />
      </ToolButton>
      <ToolButton title="Collapse all columns" onClick={props.onCollapseAll}>
        <ChevronsDownUp className="h-4 w-4" />
      </ToolButton>
      <ToolButton title="Expand all columns" onClick={props.onExpandAll}>
        <ChevronsUpDown className="h-4 w-4" />
      </ToolButton>
      <ToolButton title="Unhide all cards" disabled={!props.hasHidden} onClick={props.onUnhideAll}>
        <Eye className="h-4 w-4" />
      </ToolButton>
      <Divider />

      <div className="flex items-center gap-1 pl-0.5 pr-1">
        <span className="text-[11px] text-faint">Lens</span>
        <Select value={props.lens} onValueChange={(v) => props.onLensChange(v as LensId)}>
          <SelectTrigger className="h-7 w-[150px] text-[12px]"><SelectValue /></SelectTrigger>
          <SelectContent>
            {LENS_ORDER.map((id) => (
              <SelectItem key={id} value={id}>{LENSES[id].label}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
    </div>
  );
}

function FilterDropdown({
  icon,
  label,
  items,
  isChecked,
  onToggle,
  activeCount,
  emptyHint,
}: {
  icon: React.ReactNode;
  label: string;
  items: { key: string; label: string }[];
  isChecked: (key: string) => boolean;
  onToggle: (key: string) => void;
  activeCount: number;
  emptyHint: string;
}) {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    window.addEventListener("click", close);
    return () => window.removeEventListener("click", close);
  }, [open]);

  return (
    <div className="relative" onClick={(e) => e.stopPropagation()}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex h-7 items-center gap-1 rounded px-2 text-[12px] text-foreground/80 hover:bg-panel2"
      >
        {icon}
        {label}
        {activeCount > 0 && (
          <span className="grid h-4 min-w-4 place-items-center rounded-full bg-brand/15 px-1 text-[9px] font-semibold text-brand">
            {activeCount}
          </span>
        )}
        <ChevronDown className="h-3 w-3 text-faint" />
      </button>
      {open && (
        <div className="absolute bottom-full left-0 mb-1 max-h-64 min-w-[180px] overflow-y-auto rounded-md border border-line bg-panel py-1 shadow-card">
          {items.length === 0 ? (
            <div className="px-3 py-2 text-[12px] text-faint">{emptyHint}</div>
          ) : (
            items.map((it) => (
              <label
                key={it.key}
                className="flex cursor-pointer items-center gap-2 px-3 py-1 text-[12px] hover:bg-panel2"
              >
                <input
                  type="checkbox"
                  checked={isChecked(it.key)}
                  onChange={() => onToggle(it.key)}
                  className="h-3.5 w-3.5 accent-[var(--brand,#0d9488)]"
                />
                <span className="truncate">{it.label}</span>
              </label>
            ))
          )}
        </div>
      )}
    </div>
  );
}

function ToolButton({
  title,
  disabled,
  onClick,
  children,
}: {
  title: string;
  disabled?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      title={title}
      disabled={disabled}
      onClick={onClick}
      className="grid h-7 w-7 place-items-center rounded text-faint hover:bg-panel2 hover:text-foreground disabled:cursor-not-allowed disabled:opacity-30"
    >
      {children}
    </button>
  );
}

function Divider() {
  return <span className="mx-0.5 h-5 w-px bg-line" />;
}
