"use client";

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
  Share2,
} from "lucide-react";
import { cn } from "@/lib/utils";

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
  onNew: () => void;
  onSave: () => void;
  onToggleAutosave: () => void;
  onUndo: () => void;
  onRedo: () => void;
  onArrange: () => void;
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
      <IconBtn onClick={p.onArrange} title="Tidy up — auto-arrange &amp; separate groups">
        <LayoutGrid className="h-4 w-4" />
      </IconBtn>
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
