import type { IntegrationSource } from "@/lib/api";
import type { ContentGroup } from "@/lib/knowledge-api";

export type SourceGroup = ContentGroup & { connection?: IntegrationSource };

export function workflowOutputLabel(group: SourceGroup): string {
  if (!group.connection) return "Process → Embed → Search & AI";
  if (group.collections.some((collection) => collection.pipeline_enabled)) return "Catalog + indexed definitions → Search & AI";
  return group.collections.length ? "Catalog; document imports run separately" : "Catalog & governance";
}

/** Explicit provenance joins a connector to its collections. Never infer from names. */
export function sourceGroups(connections: IntegrationSource[], content: ContentGroup[]): SourceGroup[] {
  const groups = new Map<string, SourceGroup>(content.map((group) => [group.id, { ...group }]));
  for (const connection of connections) {
    const id = `integration:${connection.id}`;
    groups.set(id, { ...groups.get(id), id, name: connection.name, provider: connection.source_type,
      document_count: groups.get(id)?.document_count ?? 0, collections: groups.get(id)?.collections ?? [], connection });
  }
  return [...groups.values()].sort((a, b) => a.name.localeCompare(b.name));
}
