export type TaskScope = "mine" | "unassigned" | "all";

export const TASK_REASON_UI: {
  key: string;
  label: string;
  role: "Owner" | "Steward";
  hint: string;
}[] = [
  {
    key: "UNVERIFIED",
    label: "Verify",
    role: "Owner",
    hint: "Owner work — move each to Verified, or flag it for Attention.",
  },
  {
    key: "NO_CATEGORY",
    label: "Category",
    role: "Owner",
    hint: "Owner work — give each asset a category.",
  },
  {
    key: "ATTENTION",
    label: "Attention",
    role: "Steward",
    hint: "Steward work — agree with the Owner whether it should be deleted.",
  },
  {
    key: "DELETED",
    label: "To Be Deleted",
    role: "Steward",
    hint: "Steward work — confirm the asset can go, or restore it.",
  },
];

export const TASK_PAGE_SIZE = 200;
export const TASK_DEFAULT_KIND_SCOPE = "all";

export function visibleSelectedTaskIds(
  rowIds: number[],
  selected: ReadonlySet<number>,
): number[] {
  return rowIds.filter((id) => selected.has(id));
}

export function generationPreviewKey(reasons: string[], kindScope: string): string {
  return JSON.stringify({ reasons: [...reasons].sort(), kindScope });
}

export function generationPreviewTokenRequired(kindScope: string): boolean {
  return kindScope === "singleton" || kindScope === "all";
}

export function generationPreviewAuthorizes(input: {
  dryRun: boolean | undefined;
  previewKey: string | null;
  currentKey: string;
  kindScope: string;
  previewToken: string | undefined;
}): boolean {
  return (
    input.dryRun === true &&
    input.previewKey === input.currentKey &&
    (!generationPreviewTokenRequired(input.kindScope) || !!input.previewToken)
  );
}

export function buildGenerationCommitRequest(
  reasons: string[],
  kindScope: string,
  previewToken?: string,
) {
  return {
    reasons,
    dry_run: false,
    kind_scope: kindScope,
    ...(generationPreviewTokenRequired(kindScope) && previewToken
      ? { preview_token: previewToken }
      : {}),
  };
}

export function buildTaskListParams(input: {
  scope: TaskScope;
  isAdmin: boolean;
  personId: string;
  reason: string;
  search: string;
  page: number;
}): Record<string, string | number | undefined> {
  const params: Record<string, string | number | undefined> = {
    limit: TASK_PAGE_SIZE,
    page: input.page,
    ordering: "-created_at",
    state: "open",
  };
  if (input.scope === "mine") {
    if (input.isAdmin && input.personId) params.assignee = input.personId;
    else params.scope = "mine";
  } else if (input.isAdmin) {
    params.scope = input.scope;
  } else if (!input.isAdmin) {
    params.scope = "mine";
  }
  if (input.reason) params.reason = input.reason;
  if (input.search.trim()) params.search = input.search.trim();
  return params;
}
