"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Search } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useDebounced } from "@/lib/use-debounced";
import { typeMeta, typesByCategory } from "@/lib/metrics-canvas/catalog";
import type { CatalogSection } from "@/lib/metrics-canvas/catalog-tiles";
import { MM_MIME, type DragPayload } from "@/lib/metrics-canvas/dnd";
import { PaletteCatalogSection } from "@/components/metrics-canvas/palette-catalog-section";
import { SimpleSelect } from "@/components/ui/simple-select";

const SECTIONS: CatalogSection[] = ["tables", "measures", "columns", "pages", "relationships"];

function onTypeDragStart(e: React.DragEvent, elementType: string) {
  const payload: DragPayload = { kind: "type", elementType };
  e.dataTransfer.setData(MM_MIME, JSON.stringify(payload));
  e.dataTransfer.effectAllowed = "copy";
}

function ElementsRegion() {
  const groups = typesByCategory();
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set(["Shapes", "Other"]));
  const toggle = (cat: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(cat)) next.delete(cat);
      else next.add(cat);
      return next;
    });

  return (
    <div>
      {groups.map(({ cat, keys }) => {
        const isCollapsed = collapsed.has(cat);
        return (
          <div key={cat} className="border-b border-line">
            <button
              onClick={() => toggle(cat)}
              className="flex w-full items-center gap-1.5 px-2.5 py-1.5 text-left text-[11px] font-semibold uppercase tracking-[0.04em] text-faint hover:bg-foreground/[0.03]"
            >
              {isCollapsed ? <ChevronRight className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
              <span className="flex-1">{cat}</span>
              <span className="text-[10px] font-normal text-faint">{keys.length}</span>
            </button>
            {!isCollapsed && (
              <div className="grid grid-cols-2 gap-1 px-1.5 pb-2">
                {keys.map((k) => {
                  const meta = typeMeta(k);
                  return (
                    <div
                      key={k}
                      draggable
                      onDragStart={(e) => onTypeDragStart(e, k)}
                      title={`Drag “${meta.label}” onto the canvas`}
                      className="flex cursor-grab items-center gap-1.5 rounded-r-md border-l-2 bg-panel px-1.5 py-1 hover:bg-panel2 active:cursor-grabbing"
                      style={{ borderLeftColor: meta.color }}
                    >
                      <span className="grid h-4 w-4 shrink-0 place-items-center text-[12px] leading-none" style={{ color: meta.color }}>
                        {meta.icon}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-[11px] font-medium">{meta.label}</span>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** Left palette: static element tiles on top, live catalog sections below. */
export function Palette() {
  const [tab, setTab] = useState<"catalog" | "elements">("catalog");
  const [searchInput, setSearchInput] = useState("");
  const search = useDebounced(searchInput, 350);
  const [workspace, setWorkspace] = useState("");
  const [dataset, setDataset] = useState("");

  // Datasets cascade off the selected workspace (dependent filters).
  const { data: filters } = useQuery({
    queryKey: ["mm-filters", workspace],
    queryFn: () => api.filters({ workspace_name: workspace }),
    staleTime: 5 * 60_000,
  });

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* tab switch */}
      <div className="flex shrink-0 border-b border-line">
        {(["catalog", "elements"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={cn(
              "flex-1 px-2 py-2 text-[12px] font-medium capitalize transition-colors",
              tab === t ? "border-b-2 border-brand text-foreground" : "text-muted-foreground hover:text-foreground",
            )}
          >
            {t}
          </button>
        ))}
      </div>

      {tab === "catalog" ? (
        <>
          <div className="shrink-0 space-y-1.5 border-b border-line p-2">
            <div className="flex h-8 items-center gap-2 rounded-md border border-input bg-panel px-2.5 text-[12px]">
              <Search className="h-3.5 w-3.5 text-faint" />
              <input
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                placeholder="Search catalog…"
                className="min-w-0 flex-1 bg-transparent outline-none placeholder:text-faint"
              />
            </div>
            <div className="flex gap-1.5">
              <SimpleSelect
                value={workspace}
                onValueChange={(v) => { setWorkspace(v); setDataset(""); }}
                className="h-7 w-auto min-w-0 flex-1 px-2 text-[11px]"
                options={[
                  { value: "", label: "All workspaces" },
                  ...(filters?.workspaces ?? []).map((w) => ({ value: w, label: w })),
                ]}
              />
              <SimpleSelect
                value={dataset}
                onValueChange={setDataset}
                className="h-7 w-auto min-w-0 flex-1 px-2 text-[11px]"
                options={[
                  { value: "", label: "All datasets" },
                  ...(filters?.datasets ?? []).map((d) => ({ value: d, label: d })),
                ]}
              />
            </div>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            {SECTIONS.map((s) => (
              <PaletteCatalogSection
                key={s}
                section={s}
                search={search}
                workspaceName={workspace || undefined}
                datasetName={dataset || undefined}
              />
            ))}
          </div>
        </>
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto">
          <ElementsRegion />
        </div>
      )}
    </div>
  );
}
