import { describe, expect, it } from "vitest";
import { updateGroupRows } from "@/components/dictionary/dictionary-view";
import type { Item } from "@/lib/api";

const rows: Item[] = [
  { item_id: "a", item_name: "Revenue", item_type: "PB_MEASURE", group: 10 },
  { item_id: "b", item_name: "Revenue", item_type: "PB_MEASURE", group: 10 },
  { item_id: "c", item_name: "Costs", item_type: "PB_MEASURE", group: 11 },
];

describe("dictionary group assignment cache", () => {
  it("updates every cached instance in the group and leaves neighbours untouched", () => {
    const updated = updateGroupRows(rows, 10, {
      definition: 5,
      definition_name: "Commercial Revenue",
    });

    expect(updated.slice(0, 2).every((row) => row.definition === 5)).toBe(true);
    expect(updated.slice(0, 2).every((row) => row.definition_name === "Commercial Revenue")).toBe(
      true,
    );
    expect(updated[2]).toBe(rows[2]);
  });
});
