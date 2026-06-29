/**
 * Builds the colibri-style "tree" sidebar from the loaded network nodes:
 * dbt assets grouped by database → schema, PowerBI tables by workspace →
 * dataset, each leaf a focusable asset. Pure (no React) so it is unit-tested.
 *
 * Mirrors colibri's `tree.byDatabase` (generator.py), but built from the live
 * graph rather than a precomputed manifest.
 */
import type { NetworkNode } from "@/lib/api";
import { cleanLabel } from "@/lib/lineage/graph-utils";

/** Asset node groups that become focusable leaves in the tree. */
const ASSET_GROUPS: ReadonlySet<string> = new Set([
  "DBT_MODEL",
  "DBT_SOURCE",
  "DBT_SEED",
  "PB_TABLE",
]);

export interface TreeLeaf {
  id: string; // node id, used to focus the graph
  label: string;
  group: string;
}

export interface TreeBranch {
  key: string;
  label: string;
  children: TreeBranch[];
  leaves: TreeLeaf[];
}

const UNKNOWN_DB = "(unknown)";

function ensureBranch(parent: TreeBranch, label: string): TreeBranch {
  let child = parent.children.find((c) => c.label === label);
  if (!child) {
    child = { key: `${parent.key}/${label}`, label, children: [], leaves: [] };
    parent.children.push(child);
  }
  return child;
}

function sortBranch(b: TreeBranch): void {
  b.children.sort((x, y) => x.label.localeCompare(y.label));
  b.leaves.sort((x, y) => x.label.localeCompare(y.label));
  b.children.forEach(sortBranch);
}

/**
 * Build a two-level grouping tree from the loaded nodes. Top level is the
 * "system" (dbt / Power BI), then database-or-workspace, then schema-or-dataset,
 * then the asset leaves.
 */
export function buildLineageTree(nodes: NetworkNode[]): TreeBranch[] {
  const dbt: TreeBranch = { key: "dbt", label: "dbt", children: [], leaves: [] };
  const pbi: TreeBranch = { key: "powerbi", label: "Power BI", children: [], leaves: [] };

  for (const n of nodes) {
    const group = (n.group || "").toUpperCase();
    if (!ASSET_GROUPS.has(group)) continue;
    const leaf: TreeLeaf = { id: n.id, label: cleanLabel(n.label || n.id), group };

    if (group === "PB_TABLE") {
      const workspace = (n.workspace_name as string) || "Power BI";
      const dataset = (n.dataset as string) || (n.parent as string) || UNKNOWN_DB;
      ensureBranch(ensureBranch(pbi, workspace), dataset).leaves.push(leaf);
    } else {
      const database = (n.database as string) || (n.parent as string) || UNKNOWN_DB;
      const schema = (n.schema as string) || UNKNOWN_DB;
      ensureBranch(ensureBranch(dbt, database), schema).leaves.push(leaf);
    }
  }

  const roots = [dbt, pbi].filter((r) => r.children.length > 0);
  roots.forEach(sortBranch);
  return roots;
}

/** Count of focusable leaves under a branch (recursive). */
export function countLeaves(b: TreeBranch): number {
  return b.leaves.length + b.children.reduce((n, c) => n + countLeaves(c), 0);
}

/** Folder a dbt asset falls under when no real file `path` is on the node:
 *  seeds / sources by type, models by their name prefix (covering both colibri's
 *  fact_/dim_ and the common fct_/mart_ conventions). */
function dbtFallbackFolder(n: NetworkNode): string {
  const group = (n.group || "").toUpperCase();
  if (group === "DBT_SEED") return "seeds";
  if (group === "DBT_SOURCE") return "sources";
  const name = String(n.label || n.id).split(".").pop()!.toLowerCase();
  if (name.startsWith("stg_")) return "staging";
  if (name.startsWith("int_")) return "intermediate";
  if (/^(fct_|fact_|dim_|mart_|mrt_)/.test(name)) return "marts";
  return "models";
}

/** Nested folder chain for a dbt asset: its real file `path` (filename dropped)
 *  when present, else a single synthetic folder derived from its type/prefix. */
function dbtPathChain(n: NetworkNode): string[] {
  const path = typeof n.path === "string" ? n.path : "";
  if (path) {
    const parts = path.split(/[\\/]+/).filter(Boolean);
    if (parts.length && /\.\w+$/.test(parts[parts.length - 1])) parts.pop();
    if (parts.length) return parts;
  }
  return [dbtFallbackFolder(n)];
}

/**
 * Folder/path grouping (colibri's `tree.byPath`): dbt assets nest under their
 * model folders (staging / intermediate / marts / seeds / sources or the real
 * file path), PowerBI tables under workspace → dataset. Top level is the system
 * (dbt / Power BI), matching {@link buildLineageTree}'s shape so the sidebar can
 * swap between the two groupings.
 */
export function buildLineageTreeByPath(nodes: NetworkNode[]): TreeBranch[] {
  const dbt: TreeBranch = { key: "dbt", label: "dbt", children: [], leaves: [] };
  const pbi: TreeBranch = { key: "powerbi", label: "Power BI", children: [], leaves: [] };

  for (const n of nodes) {
    const group = (n.group || "").toUpperCase();
    if (!ASSET_GROUPS.has(group)) continue;
    const leaf: TreeLeaf = { id: n.id, label: cleanLabel(n.label || n.id), group };

    if (group === "PB_TABLE") {
      const workspace = (n.workspace_name as string) || "Power BI";
      const dataset = (n.dataset as string) || (n.parent as string) || UNKNOWN_DB;
      ensureBranch(ensureBranch(pbi, workspace), dataset).leaves.push(leaf);
    } else {
      let branch = dbt;
      for (const seg of dbtPathChain(n)) branch = ensureBranch(branch, seg);
      branch.leaves.push(leaf);
    }
  }

  const roots = [dbt, pbi].filter((r) => r.children.length > 0 || r.leaves.length > 0);
  roots.forEach(sortBranch);
  return roots;
}
