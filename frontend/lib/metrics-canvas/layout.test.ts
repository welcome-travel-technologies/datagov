import { describe, it, expect } from "vitest";
import { arrangeDagre } from "@/lib/metrics-canvas/layout";
import type { RfEdge, RfNode } from "@/lib/metrics-canvas/serialize";

function node(id: string): RfNode {
  return { id, type: "element", position: { x: 0, y: 0 }, width: 190, height: 60, data: { elementType: "measure", label: id } };
}

describe("arrangeDagre", () => {
  it("returns a finite position for every node", () => {
    const nodes = [node("a"), node("b"), node("c")];
    const edges: RfEdge[] = [
      { id: "e1", source: "a", target: "b" },
      { id: "e2", source: "b", target: "c" },
    ];
    const pos = arrangeDagre(nodes, edges, "LR");
    for (const n of nodes) {
      expect(pos[n.id]).toBeDefined();
      expect(Number.isFinite(pos[n.id].x)).toBe(true);
      expect(Number.isFinite(pos[n.id].y)).toBe(true);
    }
  });

  it("lays a→b→c out left-to-right by rank", () => {
    const nodes = [node("a"), node("b"), node("c")];
    const edges: RfEdge[] = [
      { id: "e1", source: "a", target: "b" },
      { id: "e2", source: "b", target: "c" },
    ];
    const pos = arrangeDagre(nodes, edges, "LR");
    expect(pos.a.x).toBeLessThan(pos.b.x);
    expect(pos.b.x).toBeLessThan(pos.c.x);
  });

  it("ignores self-loops and dangling edges without throwing", () => {
    const nodes = [node("a")];
    const edges: RfEdge[] = [
      { id: "e1", source: "a", target: "a" },
      { id: "e2", source: "a", target: "missing" },
    ];
    const pos = arrangeDagre(nodes, edges);
    expect(pos.a).toBeDefined();
  });

  it("leaves annotation nodes (notes) where they are", () => {
    const noteNode: RfNode = {
      id: "note",
      type: "note",
      position: { x: 5, y: 5 },
      width: 200,
      height: 80,
      data: { elementType: "text", label: "hi" },
    };
    const nodes = [node("a"), node("b"), noteNode];
    const edges: RfEdge[] = [{ id: "e1", source: "a", target: "b" }];
    const pos = arrangeDagre(nodes, edges);
    expect(pos.a).toBeDefined();
    expect(pos.b).toBeDefined();
    // Notes are not arranged — the editor keeps their hand-placed position.
    expect(pos.note).toBeUndefined();
  });

  it("keeps grouped nodes clustered so group frames don't overlap", () => {
    const nodes = ["a", "b", "c", "d"].map(node);
    const edges: RfEdge[] = [
      { id: "e1", source: "a", target: "b" },
      { id: "e2", source: "c", target: "d" },
      { id: "e3", source: "a", target: "c" },
    ];
    const groups = [
      { id: "g1", name: "G1", color: "#000", nodeIds: ["a", "b"] },
      { id: "g2", name: "G2", color: "#111", nodeIds: ["c", "d"] },
    ];
    const pos = arrangeDagre(nodes, edges, "TB", groups);

    const box = (ids: string[]) => {
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (const id of ids) {
        minX = Math.min(minX, pos[id].x);
        minY = Math.min(minY, pos[id].y);
        maxX = Math.max(maxX, pos[id].x + 190);
        maxY = Math.max(maxY, pos[id].y + 60);
      }
      return { minX, minY, maxX, maxY };
    };
    const a = box(["a", "b"]);
    const b = box(["c", "d"]);
    const intersects = a.minX < b.maxX && b.minX < a.maxX && a.minY < b.maxY && b.minY < a.maxY;
    expect(intersects).toBe(false);
  });
});
