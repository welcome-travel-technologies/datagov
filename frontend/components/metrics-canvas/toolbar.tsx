"use client";

import {
  FilePlus2,
  Undo2,
  Redo2,
  Maximize,
  Upload,
  Download,
  Image as ImageIcon,
  LayoutGrid,
  Share2,
} from "lucide-react";

export interface ToolbarProps {
  canUndo: boolean;
  canRedo: boolean;
  onNew: () => void;
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
      <IconBtn onClick={p.onUndo} title="Undo (Ctrl+Z)" disabled={!p.canUndo}>
        <Undo2 className="h-4 w-4" />
      </IconBtn>
      <IconBtn onClick={p.onRedo} title="Redo (Ctrl+Y)" disabled={!p.canRedo}>
        <Redo2 className="h-4 w-4" />
      </IconBtn>
      <IconBtn onClick={p.onArrange} title="Auto-layout">
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
