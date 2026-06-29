import { describe, it, expect } from "vitest";
import {
  buildColumnFlow,
  buildColibriFlow,
  buildAssetFlow,
  applyEdgeHighlight,
} from "@/lib/lineage/build-flow";
import { cardHeight, CARD_HEADER_H } from "@/lib/lineage/layout";
import { NODES, LINKS, CENTER } from "@/lib/lineage/__fixtures__";
import type { NetworkNode, NetworkLink } from "@/lib/api";
import type { Highlight } from "@/components/lineage/canvas/context";

describe("buildColumnFlow", () => {
  it("emits a model-card node per container and column edges", () => {
    const { nodes, edges, model } = buildColumnFlow(NODES, LINKS, CENTER, new Set());
    expect(nodes).toHaveLength(2);
    expect(nodes.every((n) => n.type === "modelCard")).toBe(true);
    expect(edges.length).toBe(2);
    expect(model.cards).toHaveLength(2);
  });

  it("drops cards/edges whose group is hidden", () => {
    const { nodes, edges } = buildColumnFlow(NODES, LINKS, CENTER, new Set(["DBT_COLUMN"]));
    // the dbt staging card disappears, leaving only the Calendar card
    expect(nodes).toHaveLength(1);
    expect(nodes[0].id).toBe("PB_TABLE::t1");
    // the bridge edge to the dbt column is gone; only the same-card edge remains
    expect(edges).toHaveLength(1);
  });

  it("collapses a card to a header-only node and re-routes its edges to the card", () => {
    const { nodes, edges } = buildColumnFlow(
      NODES,
      LINKS,
      CENTER,
      new Set(),
      new Set(["DBT_MODEL::stg1"]),
    );
    const stg = nodes.find((n) => n.id === "DBT_MODEL::stg1")!;
    expect((stg.data as { collapsed?: boolean }).collapsed).toBe(true);
    expect((stg.style as { height?: number }).height).toBe(42);
    // the bridge edge sourced from the collapsed card loses its column handle
    const bridge = edges.find((e) => e.source === "DBT_MODEL::stg1")!;
    expect(bridge.sourceHandle).toBeUndefined();
    // every column edge still carries its underlying model-edge id for highlighting
    expect((bridge.data as { modelEdgeIds: string[] }).modelEdgeIds.length).toBeGreaterThan(0);
  });

  it("draws cross-card edges as bezier and the same-card edge as a self-loop", () => {
    const { edges } = buildColumnFlow(NODES, LINKS, CENTER, new Set());
    // c1 → m1 lives inside the Calendar card; d1 → c1 crosses cards.
    const selfLoop = edges.find((e) => e.source === e.target)!;
    const cross = edges.find((e) => e.source !== e.target)!;
    expect(selfLoop.type).toBe("selfLoop");
    expect(cross.type).toBe("default");
  });
});

describe("buildColibriFlow — measure→measure lineage", () => {
  // Two measures where Total feeds Total YoY. Each measure is promoted to its own
  // card, so the dependency is a normal cross-card edge (not an intra-card loop).
  const nodes: NetworkNode[] = [
    { id: "PB_TABLE::t", group: "PB_TABLE", label: "Sales" },
    { id: "PB_MEASURE::m1", group: "PB_MEASURE", label: "Total", dataset: "ds" } as NetworkNode,
    { id: "PB_MEASURE::m2", group: "PB_MEASURE", label: "Total YoY", dataset: "ds" } as NetworkNode,
  ];
  const links: NetworkLink[] = [
    { source: "PB_TABLE::t", target: "PB_MEASURE::m1", kind: "contains" },
    { source: "PB_TABLE::t", target: "PB_MEASURE::m2", kind: "contains" },
    { source: "PB_MEASURE::m1", target: "PB_MEASURE::m2", kind: "column" },
  ];

  it("routes a measure→measure edge across two separate measure cards", () => {
    const { nodes: out, edges } = buildColibriFlow(nodes, links, null, { includeReportCards: false });
    // each measure is its own card
    expect(out.filter((n) => n.type === "measuresCard").map((n) => n.id).sort()).toEqual([
      "__measure__::PB_MEASURE::m1",
      "__measure__::PB_MEASURE::m2",
    ]);
    // the dependency is a normal cross-card edge between them, not a self-loop
    const e = edges.find(
      (x) => x.source === "__measure__::PB_MEASURE::m1" && x.target === "__measure__::PB_MEASURE::m2",
    )!;
    expect(e).toBeTruthy();
    expect(e.type).toBe("default");
    expect(e.sourceHandle).toBe("PB_MEASURE::m1");
    expect(e.targetHandle).toBe("PB_MEASURE::m2");
    expect(edges.some((x) => x.source === x.target)).toBe(false);
  });
});

describe("buildColibriFlow — focused collapse", () => {
  // Orders.customer_id → Customers.id is the connected path. Orders also has an
  // unrelated column (order_total) and Products is an entirely unrelated card.
  const nodes: NetworkNode[] = [
    { id: "PB_TABLE::a", group: "PB_TABLE", label: "Orders" },
    { id: "PB_COLUMN::ca", group: "PB_COLUMN", label: "customer_id" },
    { id: "PB_COLUMN::ca2", group: "PB_COLUMN", label: "order_total" },
    { id: "PB_TABLE::b", group: "PB_TABLE", label: "Customers" },
    { id: "PB_COLUMN::cb", group: "PB_COLUMN", label: "id" },
    { id: "PB_TABLE::c", group: "PB_TABLE", label: "Products" },
    { id: "PB_COLUMN::cc", group: "PB_COLUMN", label: "name" },
  ];
  const links: NetworkLink[] = [
    { source: "PB_TABLE::a", target: "PB_COLUMN::ca", kind: "contains" },
    { source: "PB_TABLE::a", target: "PB_COLUMN::ca2", kind: "contains" },
    { source: "PB_TABLE::b", target: "PB_COLUMN::cb", kind: "contains" },
    { source: "PB_TABLE::c", target: "PB_COLUMN::cc", kind: "contains" },
    { source: "PB_COLUMN::ca", target: "PB_COLUMN::cb", kind: "column" },
  ];
  const allCards = new Set(["PB_TABLE::a", "PB_TABLE::b", "PB_TABLE::c"]);
  const data = (n: { data: unknown }) => n.data as { collapsed?: boolean; shownColIds?: string[] };
  const height = (n: { style?: unknown }) => (n.style as { height?: number }).height;

  it("keeps only the connected columns visible when a card is collapsed with a pinned trace", () => {
    const { nodes: out } = buildColibriFlow(nodes, links, null, {
      collapsed: allCards,
      pinnedCol: "PB_COLUMN::ca",
    });
    const a = out.find((n) => n.id === "PB_TABLE::a")!;
    // Orders keeps customer_id (connected) and drops order_total (unrelated)
    expect(data(a).shownColIds).toEqual(["PB_COLUMN::ca"]);
    expect(height(a)).toBe(cardHeight(1));

    const b = out.find((n) => n.id === "PB_TABLE::b")!;
    expect(data(b).shownColIds).toEqual(["PB_COLUMN::cb"]);
  });

  it("folds a card with no connected columns to a header", () => {
    const { nodes: out } = buildColibriFlow(nodes, links, null, {
      collapsed: allCards,
      pinnedCol: "PB_COLUMN::ca",
    });
    const c = out.find((n) => n.id === "PB_TABLE::c")!;
    expect(data(c).shownColIds).toBeUndefined();
    expect(height(c)).toBe(CARD_HEADER_H);
  });

  it("keeps column handles on the connected edge across two focused-collapse cards", () => {
    const { edges } = buildColibriFlow(nodes, links, null, {
      collapsed: allCards,
      pinnedCol: "PB_COLUMN::ca",
    });
    const edge = edges.find((e) => e.source === "PB_TABLE::a" && e.target === "PB_TABLE::b")!;
    expect(edge.sourceHandle).toBe("PB_COLUMN::ca");
    expect(edge.targetHandle).toBe("PB_COLUMN::cb");
  });

  it("collapses to header-only (no focused columns) when nothing is pinned", () => {
    const { nodes: out, edges } = buildColibriFlow(nodes, links, null, { collapsed: allCards });
    const a = out.find((n) => n.id === "PB_TABLE::a")!;
    expect(data(a).shownColIds).toBeUndefined();
    expect(height(a)).toBe(CARD_HEADER_H);
    // edge re-attaches to the card (handles dropped), as before
    const edge = edges.find((e) => e.source === "PB_TABLE::a" && e.target === "PB_TABLE::b")!;
    expect(edge.sourceHandle).toBeUndefined();
    expect(edge.targetHandle).toBeUndefined();
  });

  it("ignores the pinned trace for cards that stay expanded", () => {
    const { nodes: out } = buildColibriFlow(nodes, links, null, {
      collapsed: new Set(),
      pinnedCol: "PB_COLUMN::ca",
    });
    const a = out.find((n) => n.id === "PB_TABLE::a")!;
    // expanded card shows every column and carries no focused subset
    expect(data(a).shownColIds).toBeUndefined();
    expect(height(a)).toBe(cardHeight(2));
  });
});

describe("buildColibriFlow — linkedColumnsOnly (Show full lineage)", () => {
  // Orders.customer_id → Customers.id is the only lineage edge. order_total and
  // Products.name are unconnected columns.
  const nodes: NetworkNode[] = [
    { id: "PB_TABLE::a", group: "PB_TABLE", label: "Orders" },
    { id: "PB_COLUMN::ca", group: "PB_COLUMN", label: "customer_id" },
    { id: "PB_COLUMN::ca2", group: "PB_COLUMN", label: "order_total" },
    { id: "PB_TABLE::b", group: "PB_TABLE", label: "Customers" },
    { id: "PB_COLUMN::cb", group: "PB_COLUMN", label: "id" },
    { id: "PB_TABLE::c", group: "PB_TABLE", label: "Products" },
    { id: "PB_COLUMN::cc", group: "PB_COLUMN", label: "name" },
  ];
  const links: NetworkLink[] = [
    { source: "PB_TABLE::a", target: "PB_COLUMN::ca", kind: "contains" },
    { source: "PB_TABLE::a", target: "PB_COLUMN::ca2", kind: "contains" },
    { source: "PB_TABLE::b", target: "PB_COLUMN::cb", kind: "contains" },
    { source: "PB_TABLE::c", target: "PB_COLUMN::cc", kind: "contains" },
    { source: "PB_COLUMN::ca", target: "PB_COLUMN::cb", kind: "column" },
  ];

  it("keeps only columns on the lineage, dropping unconnected ones", () => {
    const { model } = buildColibriFlow(nodes, links, null, { linkedColumnsOnly: true });
    const cols = (id: string) => model.cards.find((c) => c.id === id)!.columns.map((col) => col.id);
    expect(cols("PB_TABLE::a")).toEqual(["PB_COLUMN::ca"]); // order_total dropped
    expect(cols("PB_TABLE::b")).toEqual(["PB_COLUMN::cb"]);
    expect(cols("PB_TABLE::c")).toEqual([]); // wholly unconnected card pruned empty
  });

  it("keeps every column when the flag is off (default)", () => {
    const { model } = buildColibriFlow(nodes, links, null, {});
    const a = model.cards.find((c) => c.id === "PB_TABLE::a")!;
    expect(a.columns.map((c) => c.id)).toEqual(["PB_COLUMN::ca", "PB_COLUMN::ca2"]);
  });
});

describe("buildAssetFlow", () => {
  // a star graph: hub with 4 leaves + the center
  const center = "DBT_MODEL::center";
  const nodes: NetworkNode[] = [
    { id: center, group: "DBT_MODEL", label: "center" },
    { id: "DBT_MODEL::hub", group: "DBT_MODEL", label: "hub" },
    ...Array.from({ length: 4 }, (_, i) => ({
      id: `DBT_COLUMN::leaf${i}`,
      group: "DBT_MODEL",
      label: `leaf${i}`,
    })) as NetworkNode[],
  ];
  const links: NetworkLink[] = [
    { source: center, target: "DBT_MODEL::hub", kind: "model" },
    ...Array.from({ length: 4 }, (_, i) => ({
      source: "DBT_MODEL::hub",
      target: `DBT_COLUMN::leaf${i}`,
      kind: "model" as const,
    })),
  ];

  it("renders every visible node by default", () => {
    const { nodes: out } = buildAssetFlow(nodes, links, center, {
      hiddenGroups: new Set(),
      hiddenNodeIds: new Set(),
      hubThreshold: null,
    });
    expect(out).toHaveLength(6);
    expect(out.every((n) => n.type === "asset")).toBe(true);
  });

  it("collapses high-degree nodes into a hub above the threshold", () => {
    const { nodes: out } = buildAssetFlow(nodes, links, center, {
      hiddenGroups: new Set(),
      hiddenNodeIds: new Set(),
      hubThreshold: 3,
    });
    const hub = out.find((n) => n.id === "DBT_MODEL::hub")!;
    expect(hub.type).toBe("hub");
    // the center node is never collapsed
    expect(out.find((n) => n.id === center)!.type).toBe("asset");
  });

  it("hides a per-node-hidden node but never the center", () => {
    const { nodes: out } = buildAssetFlow(nodes, links, center, {
      hiddenGroups: new Set(),
      hiddenNodeIds: new Set(["DBT_COLUMN::leaf0", center]),
      hubThreshold: null,
    });
    expect(out.find((n) => n.id === "DBT_COLUMN::leaf0")).toBeUndefined();
    expect(out.find((n) => n.id === center)).toBeTruthy();
  });
});

describe("applyEdgeHighlight", () => {
  it("emphasizes active edges and dims the rest", () => {
    const { edges } = buildColumnFlow(NODES, LINKS, CENTER, new Set());
    const activeId = edges[0].id;
    const highlight: Highlight = {
      active: true,
      cols: new Set(),
      cards: new Set(),
      edges: new Set([activeId]),
      selectedCol: null,
    };
    const styled = applyEdgeHighlight(edges, highlight);
    const active = styled.find((e) => e.id === activeId)!;
    const inactive = styled.find((e) => e.id !== activeId)!;
    expect(active.animated).toBe(true);
    expect(active.style?.opacity).toBe(1);
    expect(inactive.style?.opacity).toBe(0.08);
  });

  it("is a no-op when no highlight is active", () => {
    const { edges } = buildColumnFlow(NODES, LINKS, CENTER, new Set());
    const styled = applyEdgeHighlight(edges, {
      active: false,
      cols: new Set(),
      cards: new Set(),
      edges: new Set(),
      selectedCol: null,
    });
    expect(styled).toBe(edges);
  });
});
