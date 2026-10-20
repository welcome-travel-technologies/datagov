import type { AdvancedView } from "@/lib/integrations-overview-api";

export type IntegrationsTab = "overview" | "content" | "advanced";

export const ADVANCED_VIEWS: AdvancedView[] = ["workflow", "sources", "destinations", "alerts", "bot"];

/** Map a `?tab=` value to the visible tab. Legacy values (workflow, sources, destinations,
 * alerts, bot) open the matching Advanced view so old bookmarks land where they used to. */
export function resolveTab(value: string | null | undefined): { tab: IntegrationsTab; view: AdvancedView | null } {
  if (value === "content") return { tab: "content", view: null };
  if (value === "advanced") return { tab: "advanced", view: "workflow" };
  if (value && (ADVANCED_VIEWS as string[]).includes(value)) return { tab: "advanced", view: value as AdvancedView };
  return { tab: "overview", view: null };
}
