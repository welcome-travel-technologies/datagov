/**
 * Pure helpers for reasoning about catalog `Item`s and measure-group instances.
 * Kept framework-free so both the Data Dictionary grouping pipeline and the
 * shared item-detail element can use them without a feature-to-feature import.
 */
import type { Item } from "@/lib/api";

/**
 * External-measure heuristic (used to deprioritise it as a group representative
 * and to label instances): empty DAX, or DAX/description explicitly flagged as
 * an external measure.
 */
export function isExternalMeasure(row: Item): boolean {
  const expr = (row.expression || "").trim();
  if (!expr) return true;
  const hay = (expr + " " + (row.description || "")).toLowerCase();
  return /external\s*measure/.test(hay);
}

/** Representative workspace priority: finance > commercial > ops > marketing. */
export function wsPriority(row: Item): number {
  const ws = (row.workspace_name || "").toLowerCase();
  if (ws.indexOf("finance") !== -1) return 0;
  if (ws.indexOf("commercial") !== -1) return 1;
  if (ws.indexOf("ops") !== -1 || ws.indexOf("operation") !== -1) return 2;
  if (ws.indexOf("marketing") !== -1) return 3;
  return 4;
}

/**
 * Order group instances for display: internal before external, then workspace
 * priority, then dataset name. Mirrors the legacy Details modal ordering.
 */
export function sortInstances(instances: Item[]): Item[] {
  return instances.slice().sort((a, b) => {
    const ax = isExternalMeasure(a) ? 1 : 0;
    const bx = isExternalMeasure(b) ? 1 : 0;
    if (ax !== bx) return ax - bx;
    const ap = wsPriority(a);
    const bp = wsPriority(b);
    if (ap !== bp) return ap - bp;
    return (a.dataset_name || "") < (b.dataset_name || "") ? -1 : 1;
  });
}
