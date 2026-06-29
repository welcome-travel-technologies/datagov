import { describe, it, expect } from "vitest";
import { layoutColumnCards, layoutPathLR, cardHeight, CARD_HEADER_H, ROW_H } from "@/lib/lineage/layout";
import { buildColumnModel } from "@/lib/lineage/column-model";
import { NODES, LINKS, CENTER } from "@/lib/lineage/__fixtures__";
import type { NetworkNode, NetworkLink } from "@/lib/api";

describe("cardHeight", () => {
  it("grows with the column count", () => {
    expect(cardHeight(1)).toBe(CARD_HEADER_H + ROW_H + 8);
    expect(cardHeight(3)).toBe(CARD_HEADER_H + 3 * ROW_H + 8);
    // a 0-column card still reserves one row
    expect(cardHeight(0)).toBe(cardHeight(1));
  });
});

describe("layoutColumnCards", () => {
  it("places upstream cards left of downstream cards (longest-path LR)", () => {
    const model = buildColumnModel(NODES, LINKS, CENTER);
    const pos = layoutColumnCards(model);
    // the dbt staging model feeds the calendar table -> must sit to its left
    expect(pos["DBT_MODEL::stg1"].x).toBeLessThan(pos["PB_TABLE::t1"].x);
  });

  it("vertically centres a short level against a tall one", () => {
    // T1 (5 columns) feeds T2 (1 column): T1 sits at level 0, T2 at level 1.
    const nodes: NetworkNode[] = [
      { id: "PB_TABLE::T1", group: "PB_TABLE", label: "T1" },
      ...Array.from({ length: 5 }, (_, i) => ({
        id: `PB_COLUMN::b${i}`,
        group: "PB_COLUMN",
        label: `b${i}`,
      })),
      { id: "PB_TABLE::T2", group: "PB_TABLE", label: "T2" },
      { id: "PB_COLUMN::s0", group: "PB_COLUMN", label: "s0" },
    ];
    const links: NetworkLink[] = [
      ...Array.from({ length: 5 }, (_, i) => ({
        source: "PB_TABLE::T1",
        target: `PB_COLUMN::b${i}`,
        kind: "contains",
      })),
      { source: "PB_TABLE::T2", target: "PB_COLUMN::s0", kind: "contains" },
      { source: "PB_COLUMN::b0", target: "PB_COLUMN::s0", kind: "column" },
    ];
    const pos = layoutColumnCards(buildColumnModel(nodes, links, null));
    // The tall level anchors at the top; the short level is pushed down so both
    // levels share a vertical midline (no more "pinned to the corner" gap).
    expect(pos["PB_TABLE::T1"].y).toBe(0);
    expect(pos["PB_TABLE::T2"].y).toBe((cardHeight(5) - cardHeight(1)) / 2);
    const centerT1 = pos["PB_TABLE::T1"].y + cardHeight(5) / 2;
    const centerT2 = pos["PB_TABLE::T2"].y + cardHeight(1) / 2;
    expect(centerT1).toBe(centerT2);
  });

  it("packs stacked header-only report cards tighter than model cards", () => {
    // A measure feeding two visuals: both visuals land in the downstream level
    // as header-only report cards. PowerBI "full lineage" can stack dozens of
    // these, so they pack with a small gap rather than the full COL_GAP that
    // would leave the column sparse and far-apart.
    const nodes: NetworkNode[] = [
      { id: "PB_MEASURE::m0", group: "PB_MEASURE", label: "M0", dataset: "DS" } as NetworkNode,
      { id: "PB_VISUAL::v0", group: "PB_VISUAL", label: "V0" },
      { id: "PB_VISUAL::v1", group: "PB_VISUAL", label: "V1" },
    ];
    const links: NetworkLink[] = [
      { source: "PB_MEASURE::m0", target: "PB_VISUAL::v0", kind: "model" },
      { source: "PB_MEASURE::m0", target: "PB_VISUAL::v1", kind: "model" },
    ];
    const model = buildColumnModel(nodes, links, "PB_MEASURE::m0", {
      measuresAsOwnCards: true,
      includeReportCards: true,
    });
    const VH = CARD_HEADER_H + 6; // rendered height of a header-only report card
    const heights = { "PB_VISUAL::v0": VH, "PB_VISUAL::v1": VH };
    const pos = layoutColumnCards(model, new Set(), heights);

    expect(pos["PB_VISUAL::v0"].x).toBe(pos["PB_VISUAL::v1"].x); // same level
    const gap = pos["PB_VISUAL::v1"].y - pos["PB_VISUAL::v0"].y - VH;
    expect(gap).toBe(18); // REPORT_GAP — tighter than the 44px COL_GAP
  });
});

describe("layoutPathLR", () => {
  it("assigns increasing x by hop distance from the source", () => {
    const nodes = [
      { id: "a", group: "DBT_MODEL", label: "a" },
      { id: "b", group: "DBT_MODEL", label: "b" },
      { id: "c", group: "DBT_MODEL", label: "c" },
    ];
    const links = [
      { source: "a", target: "b", kind: "model" },
      { source: "b", target: "c", kind: "model" },
    ];
    const pos = layoutPathLR(nodes, links, "a");
    expect(pos.a.x).toBe(0);
    expect(pos.b.x).toBeGreaterThan(pos.a.x);
    expect(pos.c.x).toBeGreaterThan(pos.b.x);
  });
});
