"use client";

import { useEffect } from "react";
import { GitBranch, Maximize2, EyeOff, Copy } from "lucide-react";

export type ColumnMenuAction = "show-lineage" | "unfold" | "hide-unrelated" | "copy-dbt";

export interface ColumnMenuState {
  x: number;
  y: number;
  colId: string;
  /** Whether a dbt build command is available for this column's model. */
  canCopyDbt: boolean;
}

const ITEMS: { id: ColumnMenuAction; label: string; icon: typeof GitBranch }[] = [
  { id: "show-lineage", label: "Show full column lineage", icon: GitBranch },
  { id: "unfold", label: "Unfold all related to column", icon: Maximize2 },
  { id: "hide-unrelated", label: "Hide unrelated to column", icon: EyeOff },
  { id: "copy-dbt", label: "Copy dbt command for dependent models", icon: Copy },
];

export function ColumnContextMenu({
  menu,
  onAction,
  onClose,
}: {
  menu: ColumnMenuState | null;
  onAction: (action: ColumnMenuAction, colId: string) => void;
  onClose: () => void;
}) {
  useEffect(() => {
    if (!menu) return;
    const close = () => onClose();
    window.addEventListener("click", close);
    window.addEventListener("scroll", close, true);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("scroll", close, true);
    };
  }, [menu, onClose]);

  if (!menu) return null;

  return (
    <div
      className="fixed z-50 min-w-[240px] overflow-hidden rounded-md border border-line bg-panel py-1 shadow-card"
      style={{ left: menu.x, top: menu.y }}
      onClick={(e) => e.stopPropagation()}
    >
      {ITEMS.map(({ id, label, icon: Icon }) => {
        const disabled = id === "copy-dbt" && !menu.canCopyDbt;
        return (
          <button
            key={id}
            type="button"
            disabled={disabled}
            onClick={() => {
              onAction(id, menu.colId);
              onClose();
            }}
            className="flex w-full items-center gap-2.5 px-3 py-1.5 text-left text-[12.5px] text-foreground/90 hover:bg-panel2 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <Icon className="h-3.5 w-3.5 shrink-0 text-faint" />
            {label}
          </button>
        );
      })}
    </div>
  );
}
