import { describe, expect, it } from "vitest";
import { pivotCellKey } from "@/components/dashboard/dashboard-view";

describe("dashboard pivot coordinates", () => {
  it("does not collide when row and column labels contain spaces", () => {
    expect(pivotCellKey("A", "B C")).not.toBe(pivotCellKey("A B", "C"));
  });

  it("is deterministic for the same coordinate", () => {
    expect(pivotCellKey("Revenue Management", "Athens Workspace")).toBe(
      pivotCellKey("Revenue Management", "Athens Workspace"),
    );
  });
});
