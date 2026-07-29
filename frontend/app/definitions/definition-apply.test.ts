import { describe, expect, it } from "vitest";
import {
  buildDefinitionCommitRequest,
  buildDefinitionPreviewRequest,
  definitionApplyContextKey,
  definitionApplyPreviewAuthorizes,
  definitionApplyResponseIsCurrent,
} from "@/app/definitions/definition-apply";

describe("Definition Apply preview contract", () => {
  const base = {
    definitionId: 7,
    ownerId: 11,
    departmentId: 13,
    fields: ["ownership_person", "ownership_department"] as const,
    groupIds: [5, 3, 9],
  };

  it("keys the exact owner, department, selected fields, and membership", () => {
    const key = definitionApplyContextKey(base);

    expect(
      definitionApplyContextKey({ ...base, groupIds: [9, 5, 3] }),
    ).toBe(key);
    expect(
      definitionApplyContextKey({ ...base, ownerId: 12 }),
    ).not.toBe(key);
    expect(
      definitionApplyContextKey({ ...base, departmentId: 14 }),
    ).not.toBe(key);
    expect(
      definitionApplyContextKey({
        ...base,
        fields: ["ownership_person"],
      }),
    ).not.toBe(key);
    expect(
      definitionApplyContextKey({ ...base, groupIds: [3, 5, 10] }),
    ).not.toBe(key);
  });

  it("does not mistake equal counts or duplicate ids for exact membership", () => {
    expect(
      definitionApplyContextKey({ ...base, groupIds: [3, 5, 9] }),
    ).not.toBe(
      definitionApplyContextKey({ ...base, groupIds: [3, 5, 10] }),
    );
    expect(
      definitionApplyContextKey({ ...base, groupIds: [9, 3, 5, 5] }),
    ).toBe(definitionApplyContextKey(base));
  });

  it("requires a successful matching preview and a server token", () => {
    const currentKey = definitionApplyContextKey(base);

    expect(
      definitionApplyPreviewAuthorizes({
        dryRun: true,
        previewKey: currentKey,
        currentKey,
        previewToken: "signed-preview",
      }),
    ).toBe(true);
    expect(
      definitionApplyPreviewAuthorizes({
        dryRun: true,
        previewKey: currentKey,
        currentKey,
        previewToken: undefined,
      }),
    ).toBe(false);
    expect(
      definitionApplyPreviewAuthorizes({
        dryRun: true,
        previewKey: definitionApplyContextKey({ ...base, groupIds: [3] }),
        currentKey,
        previewToken: "signed-preview",
      }),
    ).toBe(false);
  });

  it("rejects stale in-flight responses by both key and epoch", () => {
    const currentKey = definitionApplyContextKey(base);

    expect(
      definitionApplyResponseIsCurrent({
        requestKey: currentKey,
        currentKey,
        requestEpoch: 4,
        currentEpoch: 4,
      }),
    ).toBe(true);
    expect(
      definitionApplyResponseIsCurrent({
        requestKey: currentKey,
        currentKey,
        requestEpoch: 3,
        currentEpoch: 4,
      }),
    ).toBe(false);
    expect(
      definitionApplyResponseIsCurrent({
        requestKey: definitionApplyContextKey({ ...base, ownerId: 99 }),
        currentKey,
        requestEpoch: 4,
        currentEpoch: 4,
      }),
    ).toBe(false);
  });

  it("sends selected fields on preview and echoes the token on commit", () => {
    expect(
      buildDefinitionPreviewRequest(["ownership_department"]),
    ).toEqual({
      dry_run: true,
      fields: ["ownership_department"],
    });
    expect(
      buildDefinitionCommitRequest(
        ["ownership_department", "ownership_person"],
        "signed-preview",
      ),
    ).toEqual({
      dry_run: false,
      fields: ["ownership_person", "ownership_department"],
      preview_token: "signed-preview",
    });
  });
});
