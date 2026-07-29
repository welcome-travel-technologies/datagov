import { describe, expect, it } from "vitest";
import {
  buildGenerationCommitRequest,
  buildTaskListParams,
  generationPreviewAuthorizes,
  generationPreviewKey,
  TASK_DEFAULT_KIND_SCOPE,
  TASK_REASON_UI,
  visibleSelectedTaskIds,
} from "@/app/tasks/task-manager-state";

describe("Task Manager policy", () => {
  it("defaults to the guarded all-assets generation scope", () => {
    expect(TASK_DEFAULT_KIND_SCOPE).toBe("all");
  });

  it("shows the required Owner and Steward routing", () => {
    const roles = Object.fromEntries(TASK_REASON_UI.map((reason) => [reason.key, reason.role]));
    expect(roles).toMatchObject({
      UNVERIFIED: "Owner",
      ATTENTION: "Steward",
      NO_CATEGORY: "Owner",
    });
  });

  it("pins a non-admin to linked Mine and open tasks", () => {
    const params = buildTaskListParams({
      scope: "all",
      isAdmin: false,
      personId: "999",
      reason: "",
      search: "",
      page: 4,
    });
    expect(params).toMatchObject({ scope: "mine", state: "open", page: 4 });
    expect(params).not.toHaveProperty("assignee");
  });

  it("allows an admin-only explicit person selection", () => {
    const params = buildTaskListParams({
      scope: "mine",
      isAdmin: true,
      personId: "42",
      reason: "UNVERIFIED",
      search: " Revenue ",
      page: 1,
    });
    expect(params).toMatchObject({
      assignee: "42",
      state: "open",
      reason: "UNVERIFIED",
      search: "Revenue",
    });
  });

  it("sends the admin Everyone scope explicitly", () => {
    const params = buildTaskListParams({
      scope: "all",
      isAdmin: true,
      personId: "",
      reason: "",
      search: "",
      page: 1,
    });
    expect(params).toMatchObject({ scope: "all", state: "open" });
  });

  it("limits bulk selection to rows visible on the current page", () => {
    const selectedAcrossPages = new Set([1, 2, 201, 202]);

    expect(visibleSelectedTaskIds([201, 202, 203], selectedAcrossPages)).toEqual([
      201,
      202,
    ]);
  });

  it("matches previews independent of reason order but not scope", () => {
    expect(generationPreviewKey(["ATTENTION", "UNVERIFIED"], "measure_name")).toBe(
      generationPreviewKey(["UNVERIFIED", "ATTENTION"], "measure_name"),
    );
    expect(generationPreviewKey(["UNVERIFIED"], "measure_name")).not.toBe(
      generationPreviewKey(["UNVERIFIED"], "all"),
    );
  });

  it("requires a server preview token for singleton and all scopes", () => {
    for (const kindScope of ["singleton", "all"]) {
      const currentKey = generationPreviewKey(["UNVERIFIED"], kindScope);
      const base = { dryRun: true, previewKey: currentKey, currentKey, kindScope };
      expect(generationPreviewAuthorizes({ ...base, previewToken: undefined })).toBe(false);
      expect(generationPreviewAuthorizes({ ...base, previewToken: "signed-preview" })).toBe(true);
    }

    const measureKey = generationPreviewKey(["UNVERIFIED"], "measure_name");
    expect(
      generationPreviewAuthorizes({
        dryRun: true,
        previewKey: measureKey,
        currentKey: measureKey,
        kindScope: "measure_name",
        previewToken: undefined,
      }),
    ).toBe(true);
    expect(
      generationPreviewAuthorizes({
        dryRun: true,
        previewKey: generationPreviewKey(["ATTENTION"], "all"),
        currentKey: generationPreviewKey(["UNVERIFIED"], "all"),
        kindScope: "all",
        previewToken: "signed-preview",
      }),
    ).toBe(false);
  });

  it("echoes the broad-scope token only on the matching commit", () => {
    expect(buildGenerationCommitRequest(["ATTENTION"], "singleton", "signed-preview")).toEqual({
      reasons: ["ATTENTION"],
      dry_run: false,
      kind_scope: "singleton",
      preview_token: "signed-preview",
    });
    expect(buildGenerationCommitRequest(["ATTENTION"], "measure_name", "unused")).toEqual({
      reasons: ["ATTENTION"],
      dry_run: false,
      kind_scope: "measure_name",
    });
  });
});
