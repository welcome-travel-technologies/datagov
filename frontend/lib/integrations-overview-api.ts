import { request } from "@/lib/api";

/** How a source updates, in plain words. */
export interface OverviewHow {
  mode: "scheduled" | "manual" | "imported";
  text: string;
  next_run_at: string | null;
}

export type OverviewRunStatus = "success" | "partial" | "failed" | "running" | "queued" | "never";

export interface OverviewLastRun {
  status: OverviewRunStatus;
  at: string | null;
}

/** One row per source as a person thinks of it: a connector, an import group or uploads. */
export interface OverviewSource {
  key: string;
  name: string;
  provider: string;
  provider_label: string;
  kind: "connector" | "import" | "upload";
  connection_id: number | null;
  is_active: boolean;
  how: OverviewHow;
  order: number | null;
  last_run: OverviewLastRun;
  /** `visible_documents` is what this viewer can open; access rules apply to admins too. */
  counts: { documents: number | null; visible_documents: number | null; parts: number | null; catalog_items: number | null };
  needs_attention: boolean;
  attention: string;
  /** The detailed view where the problem in `attention` is fixed. */
  fix_in: AdvancedView | null;
  workflow_step: number | null;
  can_run_now: boolean;
  /** Why "Run now" is unavailable beyond being turned off or already running; empty otherwise. */
  run_blocked: string;
  detail: { updates: string; steps: string[] };
}

export interface RunOrderEvent {
  at: string;
  source_key: string | null;
  label: string;
  kind: "workflow" | "source" | "documents" | "destination";
  /** Set when the schedule fires again and again in the next day ("every hour"); it is then listed once. */
  repeat: string | null;
}

export interface WorkflowOverview {
  how: OverviewHow;
  last_run: OverviewLastRun;
  steps: { order: number; label: string; source_key: string | null }[];
  note: string;
  needs_attention: boolean;
  attention: string;
}

export interface IntegrationsOverview {
  sources: OverviewSource[];
  run_order: RunOrderEvent[];
  run_order_truncated: boolean;
  workflow: WorkflowOverview;
  documents_enabled: boolean;
  timezone: string;
  generated_at: string;
}

export const integrationsOverviewApi = {
  get: () => request<IntegrationsOverview>("/knowledge/integrations/overview/"),
};

/** Detailed admin views. Their names stay the `?tab=` values so old bookmarks keep working. */
export type AdvancedView = "workflow" | "sources" | "destinations" | "alerts" | "bot";

export interface ManageLink { label: string; tab: AdvancedView; group: string }

/** Where a source is managed: its schedule, runs and on/off switch live in Workflow; its connection
 * details (connectors) or document access (imports and uploads) live in Sources. */
export function manageLinks(source: Pick<OverviewSource, "key" | "kind">): ManageLink[] {
  return [
    { label: "Schedule & runs", tab: "workflow", group: source.key },
    { label: source.kind === "connector" ? "Connection & access" : "Access & settings", tab: "sources", group: source.key },
  ];
}

/** The manage link that fixes the source's current problem, if it has one. */
export function fixLink(source: Pick<OverviewSource, "key" | "kind" | "fix_in">): ManageLink | null {
  return manageLinks(source).find((link) => link.tab === source.fix_in) ?? null;
}

/** "312 documents in 34 parts (20 visible to you)" / "1,204 catalog items · 12 documents"; never lists the parts. */
export function describeCounts(counts: OverviewSource["counts"]): string {
  const plural = (value: number, word: string) => `${value.toLocaleString("en-US")} ${word}${value === 1 ? "" : "s"}`;
  const pieces: string[] = [];
  if (counts.catalog_items !== null) pieces.push(plural(counts.catalog_items, "catalog item"));
  if (counts.documents !== null && (counts.documents > 0 || counts.catalog_items === null)) {
    const parts = counts.parts ?? 0;
    const visible = counts.visible_documents ?? counts.documents;
    pieces.push((parts > 1 ? `${plural(counts.documents, "document")} in ${parts.toLocaleString("en-US")} parts` : plural(counts.documents, "document"))
      + (visible < counts.documents ? ` (${visible.toLocaleString("en-US")} visible to you)` : ""));
  }
  return pieces.join(" · ") || "Nothing yet";
}

/** "in 3 h" / "5 min ago" / "just now"; "—" when unknown. */
export function relativeTime(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const time = new Date(iso).getTime();
  if (Number.isNaN(time)) return "—";
  const seconds = Math.round((time - now) / 1000);
  const size = Math.abs(seconds);
  if (size < 60) return "just now";
  const amount = size < 3600 ? `${Math.round(size / 60)} min` : size < 172800 ? `${Math.round(size / 3600)} h` : `${Math.round(size / 86400)} days`;
  return seconds > 0 ? `in ${amount}` : `${amount} ago`;
}
