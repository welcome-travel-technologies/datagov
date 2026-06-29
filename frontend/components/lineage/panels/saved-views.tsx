"use client";

import { useState } from "react";
import { Bookmark, Plus, Trash2 } from "lucide-react";
import type { SavedView } from "@/lib/lineage/saved-views";

export function SavedViewsPanel({
  views,
  canSave,
  onSave,
  onLoad,
  onDelete,
}: {
  views: SavedView[];
  canSave: boolean;
  onSave: (name: string) => void;
  onLoad: (view: SavedView) => void;
  onDelete: (id: string) => void;
}) {
  const [name, setName] = useState("");

  function save() {
    const n = name.trim();
    if (!n) return;
    onSave(n);
    setName("");
  }

  return (
    <div className="border-t border-line p-2">
      <div className="mb-1.5 flex items-center gap-1.5 px-1 text-[10.5px] font-semibold uppercase tracking-[0.04em] text-faint">
        <Bookmark className="h-3 w-3" /> Saved views
      </div>
      <div className="flex items-center gap-1">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && save()}
          placeholder="Name this view…"
          disabled={!canSave}
          className="h-7 min-w-0 flex-1 rounded-md border border-line bg-panel px-2 text-[12px] outline-none focus:border-brand disabled:opacity-50"
        />
        <button
          type="button"
          title="Save current view"
          onClick={save}
          disabled={!canSave || !name.trim()}
          className="grid h-7 w-7 shrink-0 place-items-center rounded-md border border-line text-faint hover:border-brand hover:text-brand disabled:opacity-40"
        >
          <Plus className="h-3.5 w-3.5" />
        </button>
      </div>
      {views.length > 0 && (
        <div className="mt-1.5 flex flex-col gap-0.5">
          {views.map((v) => (
            <div key={v.id} className="group flex items-center gap-1 rounded px-1 hover:bg-panel2">
              <button
                type="button"
                onClick={() => onLoad(v)}
                className="min-w-0 flex-1 truncate py-1 text-left text-[12px] text-foreground/80"
                title={v.name}
              >
                {v.name}
              </button>
              <button
                type="button"
                title="Delete view"
                onClick={() => onDelete(v.id)}
                className="grid h-5 w-5 shrink-0 place-items-center rounded text-faint opacity-0 hover:text-err group-hover:opacity-100"
              >
                <Trash2 className="h-3 w-3" />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
