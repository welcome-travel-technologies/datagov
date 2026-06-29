import { describe, it, expect } from "vitest";
import { fromDoc, importJson, normalizeDoc, toDoc, type RfEdge, type RfNode } from "@/lib/metrics-canvas/serialize";
import { emptyDoc } from "@/lib/metrics-canvas/types";

const node: RfNode = {
  id: "n1",
  type: "element",
  position: { x: 12.4, y: 30.6 },
  width: 190,
  height: 60,
  data: { elementType: "measure", label: "Revenue", borderColor: "#0078D4" },
};

const edge: RfEdge = {
  id: "e1",
  source: "n1",
  target: "n2",
  type: "smoothstep",
  data: { color: "#64748b", arrowEnd: true },
  markerEnd: { type: "arrowclosed" as never, color: "#64748b", width: 16, height: 16 },
  style: { stroke: "#64748b", strokeWidth: 2 },
};

describe("toDoc / fromDoc round-trip", () => {
  it("preserves nodes, edges, groups and meta", () => {
    const state = {
      meta: { name: "My Map", description: "hi", version: "1.0" },
      viewport: { x: 5, y: 6, zoom: 1.5 },
      nodes: [node, { ...node, id: "n2", position: { x: 200, y: 0 } }],
      edges: [edge],
      groups: [{ id: "g1", name: "Group 1", color: "#0ea5e9", nodeIds: ["n1", "n2"] }],
      drawings: [],
    };
    const doc = toDoc(state);
    expect(doc.nodes).toHaveLength(2);
    expect(doc.nodes[0].position).toEqual({ x: 12, y: 31 }); // rounded
    expect(doc.edges[0].markerEnd?.type).toBe("arrowclosed");

    const back = fromDoc(doc);
    expect(back.nodes).toHaveLength(2);
    expect(back.nodes[0].data.label).toBe("Revenue");
    expect(back.edges[0].source).toBe("n1");
    expect(back.groups[0].nodeIds).toEqual(["n1", "n2"]);
    expect(back.meta.name).toBe("My Map");
  });
});

describe("normalizeDoc / importJson", () => {
  it("coerces garbage into a valid empty doc", () => {
    const d = normalizeDoc({ nodes: "nope", meta: 5 });
    expect(d.nodes).toEqual([]);
    expect(d.edges).toEqual([]);
    expect(d.meta.version).toBe(emptyDoc().meta.version);
  });

  it("drops malformed nodes/edges", () => {
    const d = normalizeDoc({
      nodes: [{ id: "ok", type: "element", position: { x: 0, y: 0 }, data: {} }, { type: "element" }],
      edges: [{ id: "e", source: "a", target: "b" }, { id: "bad" }],
    });
    expect(d.nodes).toHaveLength(1);
    expect(d.edges).toHaveLength(1);
  });

  it("importJson parses a stringified doc", () => {
    const doc = toDoc({ meta: { name: "T", version: "1.0" }, viewport: { x: 0, y: 0, zoom: 1 }, nodes: [node], edges: [], groups: [], drawings: [] });
    const parsed = importJson(JSON.stringify(doc));
    expect(parsed.nodes[0].id).toBe("n1");
  });

  it("importJson throws on invalid JSON", () => {
    expect(() => importJson("{not json")).toThrow();
  });

  it("defaults and clamps the map-wide fontScale", () => {
    expect(normalizeDoc({ meta: { name: "X" } }).meta.fontScale).toBe(1); // missing → default
    expect(normalizeDoc({ meta: { name: "X", fontScale: 1.5 } }).meta.fontScale).toBe(1.5);
    expect(normalizeDoc({ meta: { name: "X", fontScale: 99 } }).meta.fontScale).toBe(2); // clamped to max
    expect(normalizeDoc({ meta: { name: "X", fontScale: 0 } }).meta.fontScale).toBe(1); // invalid → default
    expect(normalizeDoc({ meta: { name: "X", fontScale: 0.1 } }).meta.fontScale).toBe(0.6); // clamped to min
  });

  it("round-trips fontScale through toDoc/fromDoc", () => {
    const doc = toDoc({
      meta: { name: "T", version: "1.0", fontScale: 1.4 },
      viewport: { x: 0, y: 0, zoom: 1 },
      nodes: [],
      edges: [],
      groups: [],
      drawings: [],
    });
    expect(fromDoc(doc).meta.fontScale).toBe(1.4);
  });
});
