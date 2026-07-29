import type { DefinitionApplyField } from "@/lib/api";

export const DEFINITION_APPLY_FIELDS = [
  "ownership_person",
  "ownership_department",
] as const satisfies readonly DefinitionApplyField[];

export interface DefinitionApplyContext {
  definitionId: number;
  ownerId: number | null;
  departmentId: number | null;
  fields: readonly DefinitionApplyField[];
  groupIds: readonly number[];
}

export function normalizeDefinitionApplyFields(
  fields: readonly DefinitionApplyField[],
): DefinitionApplyField[] {
  const selected = new Set(fields);
  return DEFINITION_APPLY_FIELDS.filter((field) => selected.has(field));
}

/** A local guard for the exact state the user previewed.
 *
 * The signed server token remains authoritative. This key prevents a response
 * that was started for an older owner, department, field selection, or group
 * membership from enabling Apply in the browser.
 */
export function definitionApplyContextKey(context: DefinitionApplyContext): string {
  return JSON.stringify({
    definitionId: context.definitionId,
    ownerId: context.ownerId,
    departmentId: context.departmentId,
    fields: normalizeDefinitionApplyFields(context.fields),
    groupIds: [...new Set(context.groupIds)].sort((a, b) => a - b),
  });
}

export function definitionApplyPreviewAuthorizes(input: {
  dryRun: boolean | undefined;
  previewKey: string | null;
  currentKey: string;
  previewToken: string | undefined;
}): boolean {
  return (
    input.dryRun === true &&
    input.previewKey === input.currentKey &&
    !!input.previewToken
  );
}

export function definitionApplyResponseIsCurrent(input: {
  requestKey: string;
  currentKey: string;
  requestEpoch: number;
  currentEpoch: number;
}): boolean {
  return (
    input.requestKey === input.currentKey &&
    input.requestEpoch === input.currentEpoch
  );
}

export function buildDefinitionPreviewRequest(
  fields: readonly DefinitionApplyField[],
) {
  return {
    dry_run: true as const,
    fields: normalizeDefinitionApplyFields(fields),
  };
}

export function buildDefinitionCommitRequest(
  fields: readonly DefinitionApplyField[],
  previewToken: string,
) {
  return {
    dry_run: false as const,
    fields: normalizeDefinitionApplyFields(fields),
    preview_token: previewToken,
  };
}
