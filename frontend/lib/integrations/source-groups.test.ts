import { describe, it, expect } from "vitest";
import { sourceGroups, workflowOutputLabel } from "./source-groups";
import type { IntegrationSource } from "@/lib/api";
import type { ContentGroup } from "@/lib/knowledge-api";

describe("unified sources", () => {
  it("merges by connection identity, preserves imports, and includes empty connectors", () => {
    const connections = [{ id: 1, name: "Fabric", source_type: "powerbi_fabric" }, { id: 2, name: "dbt", source_type: "dbt" }] as IntegrationSource[];
    const groups = [
      { id: "integration:1", name: "Old display name", document_count: 9, collections: [{ id: "a" }] },
      { id: "import:Fabric", name: "Fabric", document_count: 3, collections: [{ id: "b" }] },
    ] as ContentGroup[];
    const result = sourceGroups(connections, groups);
    expect(result).toHaveLength(3);
    expect(result.find((row) => row.id === "integration:1")).toMatchObject({ name: "Fabric", document_count: 9, connection: connections[0] });
    expect(result.find((row) => row.id === "integration:2")?.collections).toEqual([]);
    expect(result.find((row) => row.id === "import:Fabric")?.connection).toBeUndefined();
    expect(workflowOutputLabel(result.find((row) => row.id === "integration:1")!)).toContain("imports run separately");
    expect(workflowOutputLabel(result.find((row) => row.id === "import:Fabric")!)).toBe("Process → Embed → Search & AI");
  });
});
