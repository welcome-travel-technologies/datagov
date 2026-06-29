"use client";

import { useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { typeMeta } from "@/lib/metrics-canvas/catalog";
import {
  SECTION_ITEM_TYPE,
  tilesForSection,
  type CatalogSection,
  type PaletteTile,
} from "@/lib/metrics-canvas/catalog-tiles";
import { MM_MIME, type DragPayload } from "@/lib/metrics-canvas/dnd";

const SECTION_LABEL: Record<CatalogSection, string> = {
  tables: "Tables",
  measures: "Measures",
  columns: "Columns",
  pages: "Pages",
  relationships: "Relationships",
};

/** Which Power BI item_type a section fetches. Relationships are derived from
 *  table rows' `relationships_json`, so that section queries PB_TABLE. */
function fetchItemType(section: CatalogSection): string {
  if (section === "relationships") return "PB_TABLE";
  return SECTION_ITEM_TYPE[section];
}

function onTileDragStart(e: React.DragEvent, tile: PaletteTile) {
  const payload: DragPayload = { kind: "catalog", tile };
  e.dataTransfer.setData(MM_MIME, JSON.stringify(payload));
  e.dataTransfer.effectAllowed = "copy";
}

/**
 * One collapsible catalog group in the palette. Fetches Power BI items
 * on-demand (only when expanded) and renders them as draggable tiles.
 */
export function PaletteCatalogSection({
  section,
  search,
  workspaceName,
  datasetName,
}: {
  section: CatalogSection;
  search: string;
  workspaceName?: string;
  datasetName?: string;
}) {
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState(1);

  const params: Record<string, string | number> = {
    service: "powerbi",
    item_type: fetchItemType(section),
    page,
    ordering: "item_name",
    ...(search ? { search } : {}),
    ...(workspaceName ? { workspace_name: workspaceName } : {}),
    ...(datasetName ? { dataset_name: datasetName } : {}),
  };

  const { data, isLoading, isError, isFetching } = useQuery({
    queryKey: ["mm-palette", section, params],
    queryFn: () => api.items.list(params),
    placeholderData: keepPreviousData,
    enabled: open,
  });

  const items = data?.results ?? [];
  const tiles = tilesForSection(section, items);

  return (
    <div className="border-b border-line last:border-b-0">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-1.5 px-2.5 py-1.5 text-left text-[11px] font-semibold uppercase tracking-[0.04em] text-faint hover:bg-foreground/[0.03]"
      >
        {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
        <span className="flex-1">{SECTION_LABEL[section]}</span>
        {open && isFetching && <Loader2 className="h-3 w-3 animate-spin text-faint" />}
        {open && data && <span className="text-[10px] font-normal text-faint">{data.count}</span>}
      </button>

      {open && (
        <div className="px-1.5 pb-2">
          {isLoading && (
            <div className="flex items-center justify-center gap-2 py-3 text-[12px] text-faint">
              <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading…
            </div>
          )}
          {isError && <p className="px-2 py-3 text-[12px] text-err">Couldn’t load items.</p>}
          {!isLoading && !isError && tiles.length === 0 && (
            <p className="px-2 py-3 text-center text-[12px] text-faint">No items.</p>
          )}
          <div className="flex flex-col gap-1">
            {tiles.map((tile) => {
              const meta = typeMeta(tile.elementType);
              return (
                <div
                  key={tile.key}
                  draggable
                  onDragStart={(e) => onTileDragStart(e, tile)}
                  title={tile.tooltip || tile.label}
                  className="flex cursor-grab items-center gap-2 rounded-r-md border-l-2 bg-panel px-2 py-1 text-left hover:bg-panel2 active:cursor-grabbing"
                  style={{ borderLeftColor: meta.color }}
                >
                  <span className="grid h-4 w-4 shrink-0 place-items-center text-[12px] leading-none" style={{ color: meta.color }}>
                    {meta.icon}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[12px] font-medium leading-tight">{tile.label}</span>
                    {tile.sub && <span className="block truncate text-[10px] text-faint">{tile.sub}</span>}
                  </span>
                </div>
              );
            })}
          </div>

          {(data?.next || data?.previous) && (
            <div className="mt-1.5 flex items-center justify-between px-1 text-[11px] text-faint">
              <button
                disabled={!data?.previous}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                className={cn("rounded px-1.5 py-0.5 hover:bg-panel2", !data?.previous && "opacity-40")}
              >
                ‹ Prev
              </button>
              <button
                disabled={!data?.next}
                onClick={() => setPage((p) => p + 1)}
                className={cn("rounded px-1.5 py-0.5 hover:bg-panel2", !data?.next && "opacity-40")}
              >
                Next ›
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
