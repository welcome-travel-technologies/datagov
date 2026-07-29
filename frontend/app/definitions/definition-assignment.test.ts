import { describe, expect, it } from "vitest";
import { buildDefinitionCandidates } from "@/app/definitions/definition-assignment";
import type { Item } from "@/lib/api";

function item(index: number, group: number, definitionName?: string): Item {
  return {
    item_id: `measure-${index}`,
    item_name: `Measure ${group}`,
    item_type: "PB_MEASURE",
    group,
    definition_name: definitionName ?? null,
  };
}

describe("definition assignment candidates", () => {
  it("retains matches beyond the old 200-item boundary", () => {
    const rows = Array.from({ length: 275 }, (_, index) => item(index, index + 1));
    expect(buildDefinitionCandidates(rows)).toHaveLength(275);
  });

  it("collapses repeated measure instances to one group and preserves membership context", () => {
    const candidates = buildDefinitionCandidates([
      item(1, 7, "Revenue"),
      item(2, 7, "Revenue"),
      item(3, 8),
    ]);
    expect(candidates).toEqual([
      { id: 7, name: "Measure 7", definition: "Revenue" },
      { id: 8, name: "Measure 8", definition: null },
    ]);
  });
});
