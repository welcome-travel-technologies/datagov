"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bookmark, Network, Plus, Trash2 } from "lucide-react";
import { api, type MetricsMap } from "@/lib/api";
import { cn } from "@/lib/utils";

export const CANVAS_MAPS_KEY = ["metrics-maps", "canvas"] as const;

function fmtWhen(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function nodeCount(m: MetricsMap): number {
  return Array.isArray(m.graph?.nodes) ? m.graph!.nodes.length : 0;
}

/**
 * Bottom-pinned "Saved maps" panel. Mirrors the lineage `SavedViewsPanel`
 * (bookmark header, inline name + "+", list with hover-delete) so the two
 * explorers feel like one app. Saving needs the live canvas, so it's lifted to
 * the parent via `onSave`; the list + delete are server-backed and self-managed.
 */
export function SavedMapsPanel({
  activeId,
  canSave,
  onSave,
  onSelect,
  onDeleted,
}: {
  activeId: number | null;
  canSave: boolean;
  onSave: (name: string) => void;
  onSelect: (m: MetricsMap) => void;
  onDeleted?: (id: number) => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState("");

  const { data, isLoading } = useQuery({
    queryKey: CANVAS_MAPS_KEY,
    queryFn: () => api.metricsMaps.list({ kind: "canvas", ordering: "-updated_at" }),
  });
  const maps = data?.results ?? [];

  const delMut = useMutation({
    mutationFn: (id: number) => api.metricsMaps.remove(id),
    onSuccess: (_void, id) => {
      qc.invalidateQueries({ queryKey: CANVAS_MAPS_KEY });
      onDeleted?.(id);
    },
  });

  function save() {
    const n = name.trim();
    if (!n) return;
    onSave(n);
    setName("");
  }

  return (
    <div className="border-t border-line p-2">
      <div className="mb-1.5 flex items-center gap-1.5 px-1 text-[10.5px] font-semibold uppercase tracking-[0.04em] text-faint">
        <Bookmark className="h-3 w-3" /> Saved maps
      </div>
      <div className="flex items-center gap-1">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && save()}
          placeholder="Name this map…"
          disabled={!canSave}
          className="h-7 min-w-0 flex-1 rounded-md border border-line bg-panel px-2 text-[12px] outline-none focus:border-brand disabled:opacity-50"
        />
        <button
          type="button"
          title="Save current map"
          onClick={save}
          disabled={!canSave || !name.trim()}
          className="grid h-7 w-7 shrink-0 place-items-center rounded-md border border-line text-faint hover:border-brand hover:text-brand disabled:opacity-40"
        >
          <Plus className="h-3.5 w-3.5" />
        </button>
      </div>

      <div className="mt-1.5 max-h-44 overflow-y-auto">
        {isLoading && <p className="px-1 py-1.5 text-[11px] text-faint">Loading…</p>}
        {!isLoading && maps.length === 0 && (
          <p className="px-1 py-1.5 text-[11px] text-faint">No saved maps yet.</p>
        )}
        <div className="flex flex-col gap-0.5">
          {maps.map((m) => (
            <div
              key={m.id}
              className={cn(
                "group flex items-center gap-1 rounded px-1 hover:bg-panel2",
                m.id === activeId && "bg-brand/10",
              )}
            >
              <button
                type="button"
                onClick={() => onSelect(m)}
                title={m.name || "Untitled map"}
                className="flex min-w-0 flex-1 items-center gap-1.5 py-1 text-left"
              >
                <Network className={cn("h-3.5 w-3.5 shrink-0", m.id === activeId ? "text-brand" : "text-faint")} />
                <span className="min-w-0 flex-1">
                  <span
                    className={cn(
                      "block truncate text-[12px] leading-tight",
                      m.id === activeId ? "font-medium text-brand" : "text-foreground/80",
                    )}
                  >
                    {m.name || "Untitled map"}
                  </span>
                  <span className="block truncate text-[10px] text-faint">
                    {nodeCount(m)} nodes · {fmtWhen(m.updated_at)}
                  </span>
                </span>
              </button>
              <button
                type="button"
                title="Delete map"
                onClick={() => delMut.mutate(m.id)}
                className="grid h-5 w-5 shrink-0 place-items-center rounded text-faint opacity-0 hover:text-err group-hover:opacity-100"
              >
                <Trash2 className="h-3 w-3" />
              </button>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
