import { describe, it, expect } from "vitest";
import { buildShapeSVG, hexToRgba } from "@/lib/metrics-canvas/shapes";

describe("buildShapeSVG", () => {
  const shapes = ["cylinder", "document", "cloud", "diamond", "ellipse", "hexagon"] as const;
  for (const s of shapes) {
    it(`returns a non-empty SVG fragment for ${s}`, () => {
      const svg = buildShapeSVG(s, 140, 96, "rgba(0,0,0,0.1)", "#333", 2);
      expect(svg.length).toBeGreaterThan(0);
      expect(svg).toMatch(/(path|polygon|ellipse)/);
      expect(svg).toContain("#333");
    });
  }
  it("returns empty string for an unknown shape", () => {
    expect(buildShapeSVG("triangle", 100, 100, "#fff", "#000")).toBe("");
  });
});

describe("hexToRgba", () => {
  it("expands #rgb and applies alpha", () => {
    expect(hexToRgba("#fff", 0.5)).toBe("rgba(255,255,255,0.5)");
  });
  it("parses #rrggbb", () => {
    expect(hexToRgba("#0078D4", 1)).toBe("rgba(0,120,212,1)");
  });
  it("passes through non-hex values", () => {
    expect(hexToRgba("rgb(1,2,3)", 0.5)).toBe("rgb(1,2,3)");
    expect(hexToRgba(null)).toBe("");
    expect(hexToRgba(undefined)).toBe("");
  });
});
