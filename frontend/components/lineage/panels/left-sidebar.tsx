"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  Search,
  FolderTree,
  Database,
  SlidersHorizontal,
  Loader2,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { colorFor, memberGlyph, cleanLabel } from "@/lib/lineage/graph-utils";
import {
  buildLineageTree,
  buildLineageTreeByPath,
  countLeaves,
  type TreeBranch,
  type TreeLeaf,
} from "@/lib/lineage/tree";
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { api, type NetworkNode, type Direction } from "@/lib/api";
import type { SavedView } from "@/lib/lineage/saved-views";
import { SavedViewsPanel } from "@/components/lineage/panels/saved-views";

export type Grouping = "system" | "folder";

export interface LeftSidebarProps {
  /** Full asset directory (everything we have), independent of the loaded ego. */
  nodes: NetworkNode[];
  activeId: string | null;
  onSelect: (id: string, node?: NetworkNode | null) => void;
  grouping: Grouping;
  onGroupingChange: (g: Grouping) => void;
  // ego load settings (direction) — surfaced via a popover. "Show full lineage"
  // is always a full traversal, so there is no depth control.
  direction: Direction;
  onDirectionChange: (dir: Direction) => void;
  // include PowerBI report → visual consumer cards in the graph
  showReports: boolean;
  onShowReportsChange: (v: boolean) => void;
  // saved views
  savedViews: SavedView[];
  canSaveView: boolean;
  onSaveView: (name: string) => void;
  onLoadView: (view: SavedView) => void;
  onDeleteView: (id: string) => void;
}

export function LeftSidebar(props: LeftSidebarProps) {
  const { nodes, activeId, onSelect, grouping, onGroupingChange } = props;
  const [q, setQ] = useState("");
  const roots = useMemo(
    () => (grouping === "folder" ? buildLineageTreeByPath(nodes) : buildLineageTree(nodes)),
    [nodes, grouping],
  );
  const needle = q.trim().toLowerCase();

  // Server-backed member search: while the user types, surface matching columns /
  // measures / fields nested under their container leaf. Debounced; matches are
  // keyed by container id so each leaf can render just its own hits.
  const [memberMatches, setMemberMatches] = useState<Map<string, NetworkNode[]>>(new Map());
  const [searching, setSearching] = useState(false);

  useEffect(() => {
    // 2-char floor: single letters match almost everything and aren't useful here.
    if (needle.length < 2) {
      setMemberMatches(new Map());
      setSearching(false);
      return;
    }
    let cancelled = false;
    setSearching(true);
    const t = setTimeout(() => {
      api.network
        .memberSearch(needle)
        .then((d) => {
          if (cancelled) return;
          const m = new Map<string, NetworkNode[]>();
          for (const n of d.nodes || []) {
            const c = n.container;
            if (!c) continue;
            const arr = m.get(c);
            if (arr) arr.push(n);
            else m.set(c, [n]);
          }
          setMemberMatches(m);
        })
        .catch(() => {
          if (!cancelled) setMemberMatches(new Map());
        })
        .finally(() => {
          if (!cancelled) setSearching(false);
        });
    }, 200);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [needle]);

  return (
    <div className="flex h-full w-full min-w-0 flex-col">
      {/* header: settings + grouping toggle */}
      <div className="flex items-center justify-between border-b border-line p-2">
        <span className="px-1 text-[10.5px] font-semibold uppercase tracking-[0.04em] text-faint">Browse</span>
        <div className="flex items-center gap-1">
          <SettingsPopover
            direction={props.direction}
            onDirectionChange={props.onDirectionChange}
            showReports={props.showReports}
            onShowReportsChange={props.onShowReportsChange}
          />
          <div className="flex items-center gap-0.5 rounded-md border border-line p-0.5">
            <GroupToggle active={grouping === "folder"} title="Group by folder" onClick={() => onGroupingChange("folder")}>
              <FolderTree className="h-3.5 w-3.5" />
            </GroupToggle>
            <GroupToggle active={grouping === "system"} title="Group by database / workspace" onClick={() => onGroupingChange("system")}>
              <Database className="h-3.5 w-3.5" />
            </GroupToggle>
          </div>
        </div>
      </div>

      {/* local filter — narrows the directory and surfaces matching columns / measures */}
      <div className="relative border-b border-line p-2">
        <Search className="pointer-events-none absolute left-4 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-faint" />
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search tables, columns, measures…"
          className="h-7 w-full rounded-md border border-line bg-panel pl-7 pr-7 text-[12px] outline-none focus:border-brand"
        />
        {searching && (
          <Loader2 className="pointer-events-none absolute right-4 top-1/2 h-3.5 w-3.5 -translate-y-1/2 animate-spin text-faint" />
        )}
      </div>

      {/* tree */}
      <div className="min-h-0 flex-1 overflow-y-auto py-1 text-[12.5px]">
        {roots.length === 0 ? (
          <div className="flex h-full items-center justify-center px-3 text-center text-[12px] text-faint">
            No assets in the catalog yet.
          </div>
        ) : (
          roots.map((r) => (
            <Branch
              key={r.key}
              branch={r}
              depth={0}
              needle={needle}
              memberMatches={memberMatches}
              activeId={activeId}
              onSelect={onSelect}
            />
          ))
        )}
      </div>

      <SavedViewsPanel
        views={props.savedViews}
        canSave={props.canSaveView}
        onSave={props.onSaveView}
        onLoad={props.onLoadView}
        onDelete={props.onDeleteView}
      />
    </div>
  );
}

/** Gear popover holding the ego-load Direction control + report toggle. */
function SettingsPopover({
  direction,
  onDirectionChange,
  showReports,
  onShowReportsChange,
}: {
  direction: Direction;
  onDirectionChange: (dir: Direction) => void;
  showReports: boolean;
  onShowReportsChange: (v: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        title="Lineage settings (direction)"
        onClick={() => setOpen((o) => !o)}
        className={cn(
          "grid h-7 w-7 place-items-center rounded-md border border-line",
          open ? "bg-brand/15 text-brand" : "text-faint hover:bg-panel2",
        )}
      >
        <SlidersHorizontal className="h-3.5 w-3.5" />
      </button>
      {open && (
        <div className="absolute right-0 z-30 mt-1 w-52 rounded-md border border-line bg-panel p-3 shadow-card">
          <div className="space-y-3">
            <div>
              <label className="mb-1 block text-[10.5px] font-semibold uppercase tracking-[0.04em] text-faint">
                Direction
              </label>
              <Select value={direction} onValueChange={(v) => onDirectionChange(v as Direction)}>
                <SelectTrigger className="h-8 text-[12px]"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="both">↕ Both</SelectItem>
                  <SelectItem value="downstream">↓ Downstream</SelectItem>
                  <SelectItem value="upstream">↑ Upstream</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="flex items-start justify-between gap-3 border-t border-line pt-3">
              <div>
                <div className="text-[12px] font-medium text-foreground">Power BI visuals</div>
                <div className="text-[10.5px] leading-snug text-faint">
                  Show the report → page → visual consumers.
                </div>
              </div>
              <Switch checked={showReports} onCheckedChange={onShowReportsChange} />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function GroupToggle({
  active,
  title,
  onClick,
  children,
}: {
  active: boolean;
  title: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      className={cn(
        "grid h-6 w-6 place-items-center rounded",
        active ? "bg-brand/15 text-brand" : "text-faint hover:bg-panel2",
      )}
    >
      {children}
    </button>
  );
}

function branchMatches(
  branch: TreeBranch,
  needle: string,
  memberMatches: Map<string, NetworkNode[]>,
): boolean {
  if (!needle) return true;
  if (branch.label.toLowerCase().includes(needle)) return true;
  // A leaf qualifies on its own name OR on a column/measure hit nested under it.
  if (branch.leaves.some((l) => l.label.toLowerCase().includes(needle) || memberMatches.has(l.id))) return true;
  return branch.children.some((c) => branchMatches(c, needle, memberMatches));
}

function Branch({
  branch,
  depth,
  needle,
  memberMatches,
  activeId,
  onSelect,
}: {
  branch: TreeBranch;
  depth: number;
  needle: string;
  memberMatches: Map<string, NetworkNode[]>;
  activeId: string | null;
  onSelect: (id: string, node?: NetworkNode | null) => void;
}) {
  // Collapsed by default: only the top-level systems (dbt / Power BI) stay open.
  // A filter search force-expands the matching branches; clearing it collapses again.
  const [open, setOpen] = useState(depth < 1);
  const expanded = needle ? true : open;
  if (!branchMatches(branch, needle, memberMatches)) return null;

  const leaves: TreeLeaf[] = needle
    ? branch.leaves.filter(
        (l) =>
          l.label.toLowerCase().includes(needle) ||
          branch.label.toLowerCase().includes(needle) ||
          memberMatches.has(l.id),
      )
    : branch.leaves;

  return (
    <div>
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-1 rounded px-2 py-0.5 text-left text-faint hover:bg-panel2"
        style={{ paddingLeft: 8 + depth * 12 }}
      >
        {expanded ? <ChevronDown className="h-3 w-3 shrink-0" /> : <ChevronRight className="h-3 w-3 shrink-0" />}
        <span className="min-w-0 truncate text-[10.5px] font-medium uppercase tracking-[0.04em]">{branch.label}</span>
        <span className="ml-auto shrink-0 pr-1 text-[10px] text-faint/70">{countLeaves(branch)}</span>
      </button>
      {expanded && (
        <div>
          {branch.children.map((c) => (
            <Branch
              key={c.key}
              branch={c}
              depth={depth + 1}
              needle={needle}
              memberMatches={memberMatches}
              activeId={activeId}
              onSelect={onSelect}
            />
          ))}
          {leaves.map((leaf) => (
            <LeafItem
              key={leaf.id}
              leaf={leaf}
              depth={depth}
              matchedMembers={needle ? memberMatches.get(leaf.id) ?? null : null}
              activeId={activeId}
              onSelect={onSelect}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * A model/table row in the directory. Clicking the label focuses it in the
 * canvas; the chevron lazily fetches its columns / measures (one request, cached)
 * and lists them as a deeper level, each clickable to focus that column.
 *
 * During a search, `matchedMembers` holds the column/measure hits that belong to
 * this leaf — they are shown auto-expanded so the matching members are visible
 * without a manual click, and the lazy-fetch toggle is suspended.
 */
function LeafItem({
  leaf,
  depth,
  matchedMembers,
  activeId,
  onSelect,
}: {
  leaf: TreeLeaf;
  depth: number;
  matchedMembers?: NetworkNode[] | null;
  activeId: string | null;
  onSelect: (id: string, node?: NetworkNode | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [members, setMembers] = useState<NetworkNode[] | null>(null);
  const [loading, setLoading] = useState(false);
  const indent = 14 + (depth + 1) * 12;

  // Search hits take over the member list and force the row open; otherwise the
  // chevron drives a one-shot lazy fetch of the full member set.
  const searchActive = Array.isArray(matchedMembers) && matchedMembers.length > 0;
  const shownMembers = searchActive ? matchedMembers : members;
  const isOpen = searchActive || open;

  function toggle() {
    if (searchActive) return; // expansion is driven by the search results
    const next = !open;
    setOpen(next);
    if (next && members === null && !loading) {
      setLoading(true);
      api.network
        .members(leaf.id)
        .then((d) => setMembers(d.nodes || []))
        .catch(() => setMembers([]))
        .finally(() => setLoading(false));
    }
  }

  return (
    <div>
      <div
        className={cn(
          "flex w-full items-center rounded hover:bg-panel2",
          leaf.id === activeId ? "bg-brand/10 text-brand" : "text-foreground/80",
        )}
        style={{ paddingLeft: indent }}
      >
        <button
          type="button"
          onClick={toggle}
          title={isOpen ? "Hide columns" : "Show columns"}
          className="grid h-5 w-4 shrink-0 place-items-center text-faint hover:text-foreground"
        >
          {loading ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : isOpen ? (
            <ChevronDown className="h-3 w-3" />
          ) : (
            <ChevronRight className="h-3 w-3" />
          )}
        </button>
        <button
          type="button"
          onClick={() => onSelect(leaf.id)}
          title={leaf.label}
          className={cn("flex min-w-0 flex-1 items-center gap-1.5 py-0.5 text-left", leaf.id === activeId && "font-medium")}
        >
          <span className="h-2 w-2 shrink-0 rounded-[2px]" style={{ background: colorFor(leaf.group) }} />
          <span className="min-w-0 truncate">{leaf.label}</span>
        </button>
      </div>
      {isOpen && shownMembers && (
        <div>
          {shownMembers.length === 0 ? (
            <div className="py-0.5 text-[11px] italic text-faint" style={{ paddingLeft: indent + 18 }}>
              No columns
            </div>
          ) : (
            shownMembers.map((m) => (
              <button
                key={m.id}
                type="button"
                onClick={() => onSelect(m.id, m)}
                title={m.label}
                className={cn(
                  "flex w-full items-center gap-1.5 rounded py-0.5 pr-2 text-left font-mono text-[11px] hover:bg-panel2",
                  m.id === activeId ? "bg-brand/10 font-medium text-brand" : "text-foreground/70",
                )}
                style={{ paddingLeft: indent + 18 }}
              >
                <span className="w-3 shrink-0 text-center text-faint">{memberGlyph(m)}</span>
                <span className="min-w-0 truncate">{cleanLabel(m.label || m.id)}</span>
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}
