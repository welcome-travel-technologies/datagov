import { describe, it, expect } from "vitest";
import { mergeGraph, edgeKey } from "@/lib/lineage/graph-merge";
import type { NetworkNode, NetworkLink } from "@/lib/api";

const N = (id: string, extra: Partial<NetworkNode> = {}): NetworkNode =>
  ({ id, label: id, group: "DBT_MODEL", ...extra }) as NetworkNode;

describe("edgeKey", () => {
  it("includes kind so contains and column edges don't collide", () => {
    expect(edgeKey({ source: "a", target: "b", kind: "contains" })).not.toEqual(
      edgeKey({ source: "a", target: "b", kind: "column" }),
    );
  });
});

describe("mergeGraph", () => {
  it("dedupes nodes by id and edges by (kind, source, target)", () => {
    const prevN = [N("a"), N("b")];
    const prevE: NetworkLink[] = [{ source: "a", target: "b", kind: "column" }];
    const incN = [N("b"), N("c")];
    const incE: NetworkLink[] = [
      { source: "a", target: "b", kind: "column" }, // dup
      { source: "b", target: "c", kind: "column" }, // new
    ];
    const r = mergeGraph(prevN, prevE, incN, incE);
    expect(r.nodes.map((n) => n.id).sort()).toEqual(["a", "b", "c"]);
    expect(r.links).toHaveLength(2);
  });

  it("reports only genuinely new node ids (so existing cards keep their position)", () => {
    const r = mergeGraph([N("a"), N("b")], [], [N("b"), N("c"), N("d")], []);
    expect([...r.newNodeIds].sort()).toEqual(["c", "d"]);
  });

  it("enriches an existing node with fields from the incoming payload", () => {
    const prev = [N("a", { datatype: null })];
    const inc = [N("a", { datatype: "int", parent: "tbl" })];
    const r = mergeGraph(prev, [], inc, []);
    const a = r.nodes.find((n) => n.id === "a")!;
    expect(a.datatype).toBe("int");
    expect(a.parent).toBe("tbl");
  });
});
