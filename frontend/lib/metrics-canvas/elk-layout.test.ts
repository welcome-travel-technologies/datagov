import { describe, it, expect } from "vitest";
import { arrangeElk } from "@/lib/metrics-canvas/elk-layout";
import type { RfEdge, RfNode } from "@/lib/metrics-canvas/serialize";

function node(id: string): RfNode {
  return { id, type: "element", position: { x: 0, y: 0 }, width: 190, height: 60, data: { elementType: "measure", label: id } };
}

describe("arrangeElk", () => {
  it("positions every node, routes every edge, and keeps groups apart", async () => {
    const nodes = ["a", "b", "c", "d"].map(node);
    const edges: RfEdge[] = [
      { id: "e1", source: "a", target: "b" }, // intra g1
      { id: "e2", source: "c", target: "d" }, // intra g2
      { id: "e3", source: "a", target: "c" }, // cross-group (forces g1 above g2)
    ];
    const groups = [
      { id: "g1", name: "G1", color: "#000", nodeIds: ["a", "b"] },
      { id: "g2", name: "G2", color: "#111", nodeIds: ["c", "d"] },
    ];
    const { positions, routes } = await arrangeElk(nodes, edges, groups, {
      direction: "DOWN",
      nodeSep: 100,
      rankSep: 190,
      groupSep: 160,
      stagger: false,
      staggerStep: 80,
    });

    for (const id of ["a", "b", "c", "d"]) {
      expect(positions[id]).toBeDefined();
      expect(Number.isFinite(positions[id].x) && Number.isFinite(positions[id].y)).toBe(true);
    }
    for (const id of ["e1", "e2", "e3"]) {
      expect(routes[id]?.length).toBeGreaterThanOrEqual(2);
      for (const p of routes[id]) expect(Number.isFinite(p.x) && Number.isFinite(p.y)).toBe(true);
    }

    // Group bounding boxes must not overlap (ELK compound layout).
    const box = (ids: string[]) => {
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (const id of ids) {
        minX = Math.min(minX, positions[id].x);
        minY = Math.min(minY, positions[id].y);
        maxX = Math.max(maxX, positions[id].x + 190);
        maxY = Math.max(maxY, positions[id].y + 60);
      }
      return { minX, minY, maxX, maxY };
    };
    const g1 = box(["a", "b"]), g2 = box(["c", "d"]);
    const overlap = g1.minX < g2.maxX && g2.minX < g1.maxX && g1.minY < g2.maxY && g2.minY < g1.maxY;
    expect(overlap).toBe(false);

    // The intra-g2 edge (c→d) sits at its members' absolute band — proof the
    // group offset was applied (a coordinate bug would leave it group-relative,
    // i.e. near y=0 while the members are far down the canvas).
    const cTop = Math.min(positions.c.y, positions.d.y);
    for (const p of routes.e2) expect(p.y).toBeGreaterThan(cTop - 120);
  });

  it("applies the spacing sliders to nodes INSIDE a group, not just between groups", async () => {
    // a→b is a vertical chain inside one group. The rank-spacing slider must
    // move b away from a; a regression (options only on root) leaves it pinned
    // at ELK's default (~20px) while only group-to-group gaps respond.
    const nodes = ["a", "b"].map(node);
    const edges: RfEdge[] = [{ id: "e1", source: "a", target: "b" }];
    const groups = [{ id: "g1", name: "G1", color: "#000", nodeIds: ["a", "b"] }];
    const opts = { direction: "DOWN" as const, nodeSep: 100, groupSep: 160, stagger: false, staggerStep: 80 };

    const tight = await arrangeElk(nodes, edges, groups, { ...opts, rankSep: 80 });
    const loose = await arrangeElk(nodes, edges, groups, { ...opts, rankSep: 320 });

    const gap = (p: typeof tight) => p.positions.b.y - p.positions.a.y;
    expect(gap(loose)).toBeGreaterThan(gap(tight) + 150);
  });

  it("stagger offsets alternate layers and drops baked routes", async () => {
    const nodes = ["a", "b", "c"].map(node);
    const edges: RfEdge[] = [
      { id: "e1", source: "a", target: "b" },
      { id: "e2", source: "b", target: "c" },
    ];
    const { positions, routes } = await arrangeElk(nodes, edges, [], {
      direction: "DOWN",
      nodeSep: 100,
      rankSep: 190,
      groupSep: 160,
      stagger: true,
      staggerStep: 64,
    });
    // Routes are dropped so edges float (angle) between the offset boxes.
    expect(Object.keys(routes)).toHaveLength(0);
    // a (layer 0) and c (layer 2) stay aligned; b (layer 1) is shifted sideways
    // by exactly the configured stagger amount.
    expect(positions.a.x).toBe(positions.c.x);
    expect(positions.b.x - positions.a.x).toBe(64);
  });
});
