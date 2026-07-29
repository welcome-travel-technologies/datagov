import type { Item } from "@/lib/api";

export interface DefinitionCandidate {
  id: number;
  name: string;
  definition?: string | null;
}

/** Collapse every matching item instance into its measure group. */
export function buildDefinitionCandidates(rows: Item[]): DefinitionCandidate[] {
  const byGroup = new Map<number, DefinitionCandidate>();
  for (const row of rows) {
    const groupId = row.group;
    if (!groupId || byGroup.has(groupId)) continue;
    byGroup.set(groupId, {
      id: groupId,
      name: row.item_name,
      definition: row.definition_name ?? null,
    });
  }
  return [...byGroup.values()];
}
