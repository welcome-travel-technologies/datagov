/**
 * Measure-group collapsing for the Data Dictionary, ported 1:1 from the legacy
 * `dictionary.html`. Every Item belongs to exactly one ItemGroup (exposed by
 * the serializer as `group` = item_group_id). PB_MEASURE instances sharing a
 * name live in one measure_name group; every other item is its own singleton.
 * The pipeline keys strictly by `group` so measures merge while singletons stay
 * one-row-per-item.
 */
import type { Item } from "@/lib/api";
import { isExternalMeasure, wsPriority } from "@/lib/items";

// Re-exported for existing callers; the canonical definitions live in lib/items.
export { isExternalMeasure, wsPriority };

/** A representative row for a group, carrying its instances + group metadata. */
export interface GroupedItem extends Item {
  _is_group: boolean;
  _group_count: number;
  _ws_count: number;
  _ds_count: number;
  _instances: Item[];
  _group_ids: string[];
  /** False when an active location filter matches no instance of the group. */
  _loc_match: boolean;
}

export interface LocFilters {
  ws: string;
  ds: string;
  tbl: string;
}

/**
 * Pick the default/representative instance of a group:
 *   0) the group's pinned primary (is_primary) always wins
 *   1) non-external before external
 *   2) workspace priority (finance > commercial > ops > marketing)
 *   3) deterministic tie-break (dataset, then item_id)
 */
export function pickRepresentative(instances: Item[]): Item {
  const pinned = instances.filter((x) => x.is_primary);
  const pool = pinned.length ? pinned : instances;
  return pool.slice().sort((a, b) => {
    const ax = isExternalMeasure(a) ? 1 : 0;
    const bx = isExternalMeasure(b) ? 1 : 0;
    if (ax !== bx) return ax - bx;
    const ap = wsPriority(a);
    const bp = wsPriority(b);
    if (ap !== bp) return ap - bp;
    const ad = a.dataset_name || "";
    const bd = b.dataset_name || "";
    if (ad !== bd) return ad < bd ? -1 : 1;
    return (a.item_id || "") < (b.item_id || "") ? -1 : 1;
  })[0];
}

const SURFACED_FIELDS: (keyof Item)[] = [
  "category",
  "category_name",
  "ownership_department",
  "ownership_department_name",
  "ownership_person",
  "ownership_person_name",
  "ownership_person_slack",
  "steward",
  "steward_name",
  "steward_slack",
  "custom_description",
];

/**
 * Collapse rows into one representative per ItemGroup. Governance fields stay
 * visible even if the representative lacks them: we surface the first non-empty
 * value any instance already has, so existing curation is never hidden.
 */
export function buildGroups(rows: Item[], loc: LocFilters): GroupedItem[] {
  const hasLoc = !!(loc.ws || loc.ds || loc.tbl);
  const byKey: Record<string, Item[]> = {};
  for (const r of rows) {
    const key = r.group != null ? "g:" + r.group : "i:" + r.item_id;
    (byKey[key] = byKey[key] || []).push(r);
  }

  const out: GroupedItem[] = [];
  for (const key of Object.keys(byKey)) {
    const instances = byKey[key];
    // Instances satisfying ALL active location filters.
    const cand = !hasLoc
      ? instances
      : instances.filter(
          (x) =>
            (!loc.ws || (x.workspace_name || "") === loc.ws) &&
            (!loc.ds || (x.dataset_name || "") === loc.ds) &&
            (!loc.tbl || (x.table_name || "") === loc.tbl),
        );
    const pool = hasLoc && cand.length ? cand : instances;
    const rep = pickRepresentative(pool);
    const view = { ...rep } as GroupedItem;

    for (const f of SURFACED_FIELDS) {
      if (!view[f]) {
        for (const inst of instances) {
          if (inst[f]) {
            (view as Record<string, unknown>)[f] = inst[f];
            break;
          }
        }
      }
    }
    // Status: prefer any curated (non-UNVERIFIED) status in the group.
    if (!view.status || view.status === "UNVERIFIED") {
      for (const inst of instances) {
        if (inst.status && inst.status !== "UNVERIFIED") {
          view.status = inst.status;
          break;
        }
      }
    }

    view._is_group = instances.length > 1;
    view._group_count = instances.length;
    view._ws_count = new Set(instances.map((x) => x.workspace_name || "")).size;
    view._ds_count = new Set(instances.map((x) => x.dataset_name || "")).size;
    view._instances = instances;
    view._group_ids = instances.map((x) => x.item_id);
    view._loc_match = !hasLoc || cand.length > 0;
    out.push(view);
  }
  return out;
}
