import { describe, it, expect } from "vitest";
import { upsertView, removeView, type SavedView } from "@/lib/lineage/saved-views";

const view = (id: string, createdAt: number): SavedView => ({
  id,
  name: id,
  createdAt,
  centerId: null,
  collapsed: [],
  hidden: [],
  positions: {},
  layoutMode: "auto",
  pinnedCol: null,
  lens: "lineage-type",
  layersFilter: [],
  tagsFilter: [],
  rawNodes: [],
  rawEdges: [],
});

describe("upsertView", () => {
  it("inserts newest-first", () => {
    const list = upsertView(upsertView([], view("a", 1)), view("b", 2));
    expect(list.map((v) => v.id)).toEqual(["b", "a"]);
  });

  it("replaces an existing view by id (no duplicates)", () => {
    let list = upsertView([], view("a", 1));
    list = upsertView(list, { ...view("a", 5), name: "renamed" });
    expect(list).toHaveLength(1);
    expect(list[0].name).toBe("renamed");
  });
});

describe("removeView", () => {
  it("drops the matching id", () => {
    const list = removeView([view("a", 1), view("b", 2)], "a");
    expect(list.map((v) => v.id)).toEqual(["b"]);
  });
});
