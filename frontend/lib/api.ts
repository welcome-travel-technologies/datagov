/**
 * Typed REST client for the DataGov Django backend.
 *
 * Hits Django via Next's rewrite proxy (`/api/* -> http://localhost:8000/api/*`)
 * so the browser is single-origin and the Django session + csrftoken cookies
 * flow automatically. Unsafe methods attach the `X-CSRFToken` header that
 * Django's SessionAuthentication requires.
 */
import { getCookie } from "@/lib/utils";
import type { CanvasDoc } from "@/lib/metrics-canvas/types";

const BASE = "/api";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public body?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** The message of an `ApiError`, otherwise the given fallback. */
export function getApiErrorMessage(error: unknown, fallback: string): string;
export function getApiErrorMessage(error: unknown, fallback: string | null): string | null;
export function getApiErrorMessage(error: unknown, fallback: string | null = null): string | null {
  return error instanceof ApiError ? error.message : fallback;
}

const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...(init.body ? { "Content-Type": "application/json" } : {}),
    ...((init.headers as Record<string, string>) ?? {}),
  };
  if (UNSAFE.has(method)) {
    const csrf = getCookie("csrftoken");
    if (csrf) headers["X-CSRFToken"] = csrf;
  }

  const res = await fetch(`${BASE}${path}`, {
    ...init,
    method,
    headers,
    credentials: "include",
    cache: "no-store",
  });

  if (!res.ok) {
    const body = await res.json().catch(() => ({}) as Record<string, unknown>);
    const message =
      (body as { detail?: string; error?: string })?.detail ??
      (body as { error?: string })?.error ??
      res.statusText;
    throw new ApiError(res.status, message, body);
  }
  if (res.status === 204) return undefined as T;
  const ct = res.headers.get("content-type") ?? "";
  if (ct.startsWith("text/")) return (await res.text()) as unknown as T;
  return res.json() as Promise<T>;
}

function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== null && v !== undefined && v !== "") sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

// ---- types ----------------------------------------------------------------

export interface Paginated<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}

/** Unwrap a paginated response (or pass through an array) to a plain list. */
export function unwrapResults<T>(x: { results: T[] } | T[] | undefined): T[] {
  if (!x) return [];
  return Array.isArray(x) ? x : x.results;
}

export type NodeGroup =
  | "PB_REPORT" | "PB_PAGE" | "PB_VISUAL" | "PB_TABLE" | "PB_MEASURE"
  | "PB_COLUMN" | "PB_FIELD" | "PB_WORKSPACE"
  | "DBT_MODEL" | "DBT_SOURCE" | "DBT_SEED" | "DBT_TEST" | "DBT_COLUMN"
  | "HUB" | "UNKNOWN" | string;

export type EdgeKind = "model" | "contains" | "column" | "filter" | "join" | string;

export interface NetworkNode {
  id: string;
  label: string;
  group: NodeGroup;
  datatype?: string | null;
  parent?: string | null;
  workspace_id?: string | null;
  workspace_name?: string | null;
  /** Grouping keys for the tree sidebar (dbt: database/schema; PB: workspace/dataset). */
  database?: string | null;
  schema?: string | null;
  dataset?: string | null;
  /** dbt model file path (e.g. "models/staging/stg_orders.sql"), for folder grouping. */
  path?: string | null;
  /** dbt tags / PowerBI labels, for the Tags filter. */
  tags?: string[] | null;
  /** Parent model/table id of a member node — set by member_search so a column /
   *  measure hit can be nested under its container leaf in the sidebar tree. */
  container?: string | null;
  /** Real column-lineage classification from the flow engine (column nodes). */
  lineageType?: string | null;
  /** True when the column participates in any column→column lineage edge. */
  hasLineage?: boolean;
  [k: string]: unknown;
}

export interface NetworkLink {
  source: string;
  target: string;
  kind?: EdgeKind;
  /** For column edges: how the target column was derived from the source. */
  lineage_type?: string | null;
  /** True for cross-tool (dbt ↔ PowerBI) edges. */
  bridge?: boolean;
}

export interface NetworkResponse {
  nodes: NetworkNode[];
  links: NetworkLink[];
}

export interface ReachableNode {
  id: string;
  label?: string;
  group: NodeGroup;
  distance: number;
}

export interface ReachableResponse {
  nodes: ReachableNode[];
  truncated?: boolean;
}

export interface PathResponse {
  found: boolean;
  message?: string;
  distance?: number;
  nodes: NetworkNode[];
  links: NetworkLink[];
  paths?: string[][];
}

export interface ConnectedReport {
  id?: string;
  name?: string;
  url?: string;
}

/** A semantic-model relationship surfaced on a PB_TABLE / PB_COLUMN row. */
export interface RelationshipRef {
  cardinality?: string | null;
  other_cardinality?: string | null;
  other_table?: string | null;
  other_column?: string | null;
  is_active?: boolean | null;
}

/** Governance status as stored on the ItemGroup / denormalised onto Item. */
export type ItemStatus = "UNVERIFIED" | "VERIFIED" | "DELETED" | "ATTENTION";

export interface Item {
  item_id: string;
  item_name: string;
  item_type: NodeGroup;
  type?: string | null;
  service?: string | null;
  workspace_name?: string | null;
  dataset_name?: string | null;
  table_name?: string | null;
  database_name?: string | null;
  schema_name?: string | null;
  bq_schema?: string | null;
  path?: string | null;
  datatype?: string | null;
  /** Power BI column kind, e.g. "calculated" | "data" — drives calc-column styling. */
  column_type?: string | null;
  formatstring?: string | null;
  description?: string | null;
  custom_description?: string | null;
  expression?: string | null;
  /** Compiled dbt SQL (manifest `compiled_code`); raw SQL lives in `expression`. */
  compiled_expression?: string | null;
  /** Authored dbt schema.yml properties for this node, serialized as YAML. */
  properties_yaml?: string | null;
  web_url?: string | null;
  group_id?: string | null;
  organization_name?: string | null;
  connected_reports?: number | null;
  connected_report_pages?: number | null;
  connected_visuals?: number | null;
  connected_measures?: number | null;
  connected_columns?: number | null;
  connected_tables?: number | null;
  connected_reports_json?: ConnectedReport[] | null;
  is_unused?: boolean | null;
  is_used?: boolean | null;
  is_related?: boolean | null;
  relationships_json?: RelationshipRef[] | null;
  // Governance (read-only on Item; writes go to the ItemGroup `group` pk).
  status?: ItemStatus | null;
  group?: number | null;
  group_kind?: string | null;
  is_primary?: boolean | null;
  category?: number | null;
  category_name?: string | null;
  ownership_department?: number | null;
  ownership_department_name?: string | null;
  ownership_person?: number | null;
  ownership_person_name?: string | null;
  ownership_person_slack?: string | null;
  steward?: number | null;
  steward_name?: string | null;
  steward_slack?: string | null;
  [k: string]: unknown;
}

/** Writable governance fields on an ItemGroup (PATCH /item-groups/{pk}/). */
export interface ItemGroupPatch {
  status?: ItemStatus;
  category?: number | null;
  ownership_department?: number | null;
  ownership_person?: number | null;
  steward?: number | null;
  custom_description?: string | null;
  deleted?: boolean;
}

export interface Department {
  id: number;
  name: string;
}

export interface Category {
  id: number;
  name: string;
}

export interface DataPerson {
  id: number;
  name: string;
  slack_handle?: string | null;
  is_owner?: boolean;
  is_steward?: boolean;
  is_other?: boolean;
  departments?: number[];
  department_names?: string[];
  user_email?: string | null;
}

export interface GovernanceTask {
  id: number;
  title: string;
  state: "open" | "done";
  trigger_status: "ATTENTION" | "DELETED" | string | null;
  created_at: string;
  completed_at: string | null;
  item_group: number | null;
  assignee: number | null;
  assignee_name: string | null;
  assignee_slack: string | null;
  assignee_role: string | null;
  item_name: string | null;
  asset_context: string | null;
  web_url: string | null;
}

export interface FiltersResponse {
  workspaces: string[];
  datasets: string[];
  tables: string[];
}

/** One aggregated Power BI usage row (dims vary by `group_by`). */
export interface UsageRow {
  month?: string | null;
  workspace_id?: string | null;
  workspace_name?: string | null;
  report_id?: string | null;
  report_name?: string | null;
  user_email?: string | null;
  user_display_name?: string | null;
  platform?: string | null;
  distribution_method?: string | null;
  report_page?: string | null;
  view_count: number;
  unique_users: number;
}

export interface UsageResponse {
  results: UsageRow[];
  months: string[];
  group_by: string[];
}

export interface UsageParams {
  group_by?: string;
  workspace_name?: string;
  month?: string;
  limit?: number;
}

/** Result summary returned by the governance CSV import endpoint. */
export interface GovernanceImportResult {
  message?: string;
  updated?: number;
  skipped_no_match?: { row: number; group_id: string }[];
  unmatched_values?: { row: number; field: string; value: string }[];
  ambiguous?: { row: number; field: string; value: string }[];
  invalid_status?: { row: number; value: string }[];
  error?: string;
}

export interface SummaryResponse {
  [k: string]: unknown;
}

/** One distilled measure-group record for the Dashboard KPI table + pivot. */
export interface DashboardMeasureGroup {
  w: string;
  di: number | null;
  dn: string;
  oi: number | null;
  on: string;
  si: number | null;
  sn: string;
  st: string;
  stn: string;
  ci: number | null;
  cn: string;
  h: number;
  u: number;
}

export interface DashboardWsStat {
  r: number;
  p: number;
  v30: number;
  vt: number;
}

/** Precomputed Dashboard payload (single request; aggregated server-side). */
export interface DashboardResponse {
  measure_groups: DashboardMeasureGroup[];
  ws_stats: Record<string, DashboardWsStat>;
  departments: Department[];
  owners: DataPerson[];
  stewards: DataPerson[];
  categories: Category[];
  summary: {
    total_reports: number;
    distinct_measures: number;
    unused_measures_total: number;
    views_total_all: number;
    views_recent_all: number;
    recent_month: string | null;
    recent_month_label: string;
  };
}

/** Saved-map kind. Only `canvas` is authored in the app today (`scratchpad` is
 *  retained for backward compatibility with any historical rows). */
export type MetricsMapKind = "scratchpad" | "canvas";

/** A saved, org-scoped metrics map: the visual diagram editor's `graph` document. */
export interface MetricsMap {
  id: number;
  name: string;
  description?: string;
  kind?: MetricsMapKind;
  graph?: CanvasDoc | null;
  created_by_email?: string | null;
  created_at?: string;
  updated_at?: string;
  organization?: number | null;
  /** Public share key (uuid4) when sharing is enabled; null/absent = not shared. */
  public_token?: string | null;
  /** Whether anonymous viewers of the share link may drag nodes. */
  public_can_drag?: boolean;
}

/** Fields the client is allowed to write back to a metrics map. */
export interface MetricsMapInput {
  name: string;
  description?: string;
  kind?: MetricsMapKind;
  graph?: CanvasDoc | null;
  public_can_drag?: boolean;
}

/** The anonymous, read-only projection returned by the public share endpoint. */
export interface PublicMetricsMap {
  name: string;
  description?: string;
  graph?: CanvasDoc | null;
  public_can_drag?: boolean;
}

/** Response of the `share` action (token + viewer-drag flag). */
export interface MetricsMapShareState {
  public_token: string;
  public_can_drag: boolean;
}

export interface MePerms {
  is_admin: boolean;
  can_view_dictionary: boolean;
  can_view_tasks: boolean;
  can_view_champions: boolean;
  can_view_chat: boolean;
  can_view_powerbi: boolean;
  can_view_reports: boolean;
  can_view_lineage: boolean;
  can_view_unused: boolean;
  can_view_insights: boolean;
  can_view_dbt: boolean;
  can_view_integrations: boolean;
  can_view_org_settings: boolean;
  [k: string]: boolean;
}

export interface User {
  id: number;
  email: string;
  username?: string;
  role: string;
  is_authenticated: boolean;
  perms: MePerms;
  organization?: { name?: string | null; primary_color?: string | null; icon?: string | null } | null;
}

/** Public (no-auth) org branding — name, accent colour, logo icon. Drives the
 * login screen, favicon and page title before a session exists. */
export interface Branding {
  name: string | null;
  primary_color: string | null;
  icon: string | null;
}

// ---- org admin (members + settings) ---------------------------------------

export interface OrgMember {
  user_id: number;
  email: string;
  username: string;
  display_name: string;
  is_admin: boolean;
  is_self: boolean;
  group_ids: number[];
  is_owner: boolean;
  is_steward: boolean;
  is_other: boolean;
  slack_handle: string;
  department_ids: number[];
}

export interface GroupRef {
  id: number;
  name: string;
}

export interface DepartmentRef {
  id: number;
  name: string;
}

export interface ChatbotModelRef {
  id: number;
  display_name: string;
  identifier: string;
}

export interface OrgSettings {
  /** PowerBI catalog: front-loaded listing + profiler/usage tools (local DB, no external calls). */
  powerbi_tools_enabled: boolean;
  /** PowerBI live REST DAX queries. */
  powerbi_live_tools_enabled: boolean;
  /** dbt catalog: models, columns, SQL, lineage (local DB). No live tier. */
  dbt_tools_enabled: boolean;
  /** BigQuery catalog: load dataset schema into context (read-only). */
  bigquery_tools_enabled: boolean;
  /** BigQuery live read-only SQL execution. */
  bigquery_live_tools_enabled: boolean;
  debug_responses_enabled: boolean;
  show_deleted_items: boolean;
  chatbot_model_id: number | null;
  /** Which PowerBI workspaces feed the AI Assistant context (empty = all). */
  assistant_powerbi_workspace_ids: string[];
  /** Which BigQuery datasets feed the AI Assistant context (empty = none). */
  assistant_bigquery_dataset_ids: string[];
  /** Max seconds the AI Assistant may run per question (web chat). 30–600. */
  chat_timeout_seconds: number;
}

export interface ScopeOption {
  id: string;
  name: string;
}

export interface AssistantScopeResponse {
  powerbi: ScopeOption[];
  bigquery: ScopeOption[];
}

export interface OrgMembersResponse {
  organization: { id: number; name: string; primary_color?: string | null };
  members: OrgMember[];
  available_groups: GroupRef[];
  departments: DepartmentRef[];
  chatbot_models: ChatbotModelRef[];
  settings: OrgSettings;
}

// ---- Django-Q queues (Org Settings → Queues tab) --------------------------

export interface QueueCluster {
  cluster_id: string;
  host: string;
  status: string;
  workers: number;
  task_q_size: number;
  uptime_seconds: number | null;
}

/** A task waiting in the broker (OrmQ), not yet picked up by a worker. */
export interface QueuedTask {
  id: number;
  key: string;
  task_id: string | null;
  name: string | null;
  func: string | null;
  locked: string | null;
  /** "waiting" = available to be picked up; "running" = reserved by a worker. */
  state: "waiting" | "running";
}

/** A finished task from the Django-Q Task table. */
export interface RecentTask {
  id: string;
  name: string;
  func: string;
  group: string | null;
  success: boolean;
  started: string | null;
  stopped: string | null;
  duration_seconds: number | null;
  attempt_count: number;
  short_result: string | null;
  /** Full (newline-preserving) result/reply text for the detail popup. */
  result: string | null;
}

export interface ScheduledTask {
  id: number;
  name: string | null;
  func: string;
  schedule_type: string;
  cron: string | null;
  minutes: number | null;
  repeats: number;
  next_run: string | null;
  last_success: boolean | null;
}

export interface QueuesResponse {
  online: boolean;
  clusters: QueueCluster[];
  counts: {
    queued: number;
    scheduled: number;
    success_total: number;
    failed_total: number;
    success_24h: number;
    failed_24h: number;
  };
  queued: QueuedTask[];
  recent: RecentTask[];
  schedules: ScheduledTask[];
}

/** Body for create (no user_id) or edit (with user_id) of a member. */
export interface MemberInput {
  user_id?: number;
  email?: string;
  password?: string;
  name: string;
  slack_handle?: string;
  is_owner: boolean;
  is_steward: boolean;
  is_other: boolean;
  /** Org admin flag (lives on the membership). Ignored server-side for self. */
  is_admin?: boolean;
  department_ids: number[];
  group_ids: number[];
}

// ---- integrations ---------------------------------------------------------

export interface IntegrationSchedule {
  frequency: string;
  cron_expression: string | null;
  is_enabled: boolean;
  last_run_at: string | null;
  next_run_at: string | null;
}

export interface LastRun {
  status: string;
  started_at: string;
  finished_at: string | null;
  triggered_by: string;
}

export interface IntegrationSource {
  id: number;
  name: string;
  source_type: string;
  is_active: boolean;
  tenant_id: string;
  client_id: string;
  client_secret_set: boolean;
  workspace_ids: string[];
  default_workspace_id: string;
  available_workspaces: { id?: string; name?: string }[];
  github_repo_url: string;
  github_token_set: boolean;
  github_branch: string;
  dbt_manifest_path: string;
  schedule: IntegrationSchedule | null;
  last_run: LastRun | null;
}

export interface IntegrationDestination {
  id: number;
  name: string;
  destination_type: string;
  is_active: boolean;
  bq_project_id: string;
  bq_dataset_id: string;
  bq_service_account_set: boolean;
  schedule: IntegrationSchedule | null;
  last_run: LastRun | null;
}

export interface IntegrationHook {
  id: number;
  name: string;
  hook_type: string;
  is_active: boolean;
  slack_bot_token_set: boolean;
  slack_channel: string;
  slack_alerts_channel: string;
}

export interface IntegrationsData {
  sources: IntegrationSource[];
  destinations: IntegrationDestination[];
  hooks: IntegrationHook[];
}

export interface TestResult {
  status: string;
  lines?: string[];
  [k: string]: unknown;
}

export interface RunQueued {
  status: string;
  run_log_id?: number;
  task_id?: string;
  error?: string;
}

export interface RunLog {
  id: number;
  status: string;
  started_at: string;
  finished_at: string | null;
  triggered_by: string;
  duration_seconds: number | null;
}

export interface RunLogDetail {
  id: number;
  status: string;
  started_at: string;
  finished_at: string | null;
  triggered_by: string;
  log_output: string;
}

export interface WorkflowRun {
  id: number;
  status: string;
  current_stage: string;
  triggered_by: string;
  started_at: string;
  finished_at: string | null;
  duration_seconds: number | null;
}

export interface WorkflowStepSummary {
  id: number;
  name: string;
  source_type?: string;
  destination_type?: string;
  category?: string;
  is_active: boolean;
  last_status: string | null;
}

export interface WorkflowStatus {
  schedule: IntegrationSchedule | null;
  runs: WorkflowRun[];
  sources: WorkflowStepSummary[];
  destinations: WorkflowStepSummary[];
  raw_export: { is_active: boolean; gcs_bucket_name: string; gcs_service_account_set: boolean };
}

/** Friendly schedule fields accepted by the source/destination/workflow save
 * endpoints (the backend converts daily/weekly + hour/day to a cron string). */
export interface ScheduleInput {
  schedule_frequency?: string;
  schedule_cron?: string;
  schedule_enabled?: boolean;
  schedule_hour?: string;
  schedule_day?: string;
}

export interface SourceInput extends ScheduleInput {
  id?: number;
  name?: string;
  is_active?: boolean;
  source_type?: string;
  // powerbi / fabric
  tenant_id?: string;
  client_id?: string;
  client_secret?: string;
  workspace_ids?: string;
  default_workspace_id?: string;
  // dbt / github
  github_repo_url?: string;
  github_token?: string;
  github_branch?: string;
  dbt_manifest_path?: string;
}

export interface DestinationInput extends ScheduleInput {
  id?: number;
  name?: string;
  is_active?: boolean;
  bq_dataset_id?: string;
  bq_service_account_json?: string;
}

export interface WorkflowScheduleInput {
  frequency: string;
  schedule_enabled: boolean;
  cron_expression?: string;
  schedule_hour?: string;
  schedule_day?: string;
}

export type Direction = "both" | "upstream" | "downstream";
export type LineageMode = "asset" | "column" | "unified";

// ---- per-user default workspaces (User Settings) --------------------------

export interface WorkspaceOption {
  id: string;
  name: string;
}

export interface WorkspaceSource {
  id: number;
  name: string;
  source_type: string;
  workspaces: WorkspaceOption[];
  selected_id: string;
  auto_only: boolean;
}

export interface WorkspaceDefaultsResponse {
  sources: WorkspaceSource[];
}

// ---- endpoints ------------------------------------------------------------

export const api = {
  /** Public org branding (no auth) — used on the login screen + for favicon/title. */
  branding: () => request<Branding>("/branding/"),

  auth: {
    me: () => request<User>("/me/"),
    login: (username: string, password: string) =>
      request<User>("/auth/login/", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      }),
    logout: () => request<void>("/auth/logout/", { method: "POST" }),
    /** Change the current user's password. On invalid input the backend returns
     * 400 with `{ errors }`, surfaced via `ApiError.body.errors`. */
    changePassword: (old_password: string, new_password1: string, new_password2: string) =>
      request<{ status: string }>("/me/change-password/", {
        method: "POST",
        body: JSON.stringify({ old_password, new_password1, new_password2 }),
      }),
  },

  me: {
    workspaces: () => request<WorkspaceDefaultsResponse>("/me/workspaces/"),
    saveWorkspaces: (defaults: Record<string, string>) =>
      request<WorkspaceDefaultsResponse>("/me/workspaces/", {
        method: "POST",
        body: JSON.stringify({ defaults }),
      }),
  },

  network: {
    search: (q: string, group = "") =>
      request<NetworkResponse>(`/network/${qs({ q, group })}`),
    /** Full asset directory (every model/table-level node, no columns/edges) for
     *  the sidebar browse tree. */
    assets: () => request<NetworkResponse>(`/network/${qs({ list: "assets" })}`),
    /** Lazy-load one container's members (columns / measures / fields), for
     *  expanding a model/table in the sidebar directory tree. */
    members: (parent: string) =>
      request<NetworkResponse>(`/network/${qs({ list: "members", parent })}`),
    /** Search members (columns / measures / fields) by name. Each returned node
     *  carries `container` (its parent model/table id) so the sidebar can nest
     *  the hits under the right leaf. */
    memberSearch: (q: string) =>
      request<NetworkResponse>(`/network/${qs({ list: "member_search", q })}`),
    ego: (opts: {
      node_id: string;
      depth?: number;
      direction?: Direction;
      mode?: LineageMode;
      /** column/unified modes: follow column edges to their transitive ends,
       *  ignoring `depth` (the "show full column lineage" action). */
      full?: boolean;
    }) =>
      request<NetworkResponse>(
        `/network/${qs({
          node_id: opts.node_id,
          depth: opts.depth ?? 1,
          direction: opts.direction ?? "both",
          mode: opts.mode ?? "asset",
          full: opts.full ? true : undefined,
        })}`,
      ),
    path: (opts: {
      from: string;
      to: string;
      max_depth?: number;
      direction?: Direction;
      algorithm?: string;
      workspace_id?: string;
    }) =>
      request<PathResponse>(
        `/network/path/${qs({
          from: opts.from,
          to: opts.to,
          max_depth: opts.max_depth ?? 10,
          direction: opts.direction ?? "upstream",
          algorithm: opts.algorithm ?? "all_shortest",
          workspace_id: opts.workspace_id ?? "",
        })}`,
      ),
    reachable: (opts: { from: string; direction?: Direction; workspace_id?: string }) =>
      request<ReachableResponse>(
        `/network/reachable/${qs({
          from: opts.from,
          direction: opts.direction ?? "upstream",
          workspace_id: opts.workspace_id ?? "",
        })}`,
      ),
  },

  items: {
    get: (idHash: string) => request<Item>(`/items/${encodeURIComponent(idHash)}/`),
    byName: (name: string) =>
      request<Paginated<Item>>(`/items/${qs({ item_name: name })}`),
    list: (params: Record<string, string | number | undefined> = {}) =>
      request<Paginated<Item>>(`/items/${qs(params)}`),
    /** Pin an item as its ItemGroup's primary instance. */
    setPrimary: (itemId: string) =>
      request<{ status: string; group: number; primary_item_id: number }>(
        `/items/${encodeURIComponent(itemId)}/set_primary/`,
        { method: "POST" },
      ),
  },

  /** Governance lives on the ItemGroup; PATCH here to curate a whole group. */
  itemGroups: {
    patch: (pk: number, body: ItemGroupPatch) =>
      request<Record<string, unknown>>(`/item-groups/${pk}/`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
  },

  departments: {
    list: () => request<Paginated<Department> | Department[]>("/departments/"),
  },

  categories: {
    list: () => request<Paginated<Category> | Category[]>("/categories/"),
  },

  metricsMaps: {
    list: (params: Record<string, string | number | undefined> = {}) =>
      request<Paginated<MetricsMap>>(`/metrics-maps/${qs(params)}`),
    get: (id: number) => request<MetricsMap>(`/metrics-maps/${id}/`),
    create: (body: MetricsMapInput) =>
      request<MetricsMap>("/metrics-maps/", { method: "POST", body: JSON.stringify(body) }),
    update: (id: number, body: Partial<MetricsMapInput>) =>
      request<MetricsMap>(`/metrics-maps/${id}/`, { method: "PATCH", body: JSON.stringify(body) }),
    remove: (id: number) => request<void>(`/metrics-maps/${id}/`, { method: "DELETE" }),
    // Enable / update the public share link (mints or rotates the token,
    // toggles viewer-drag). `unshare` revokes it; `publicGet` is the anonymous
    // read used by the /share/metrics-map/<token> viewer page.
    share: (id: number, body: { can_drag?: boolean; rotate?: boolean } = {}) =>
      request<MetricsMapShareState>(`/metrics-maps/${id}/share/`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
    unshare: (id: number) => request<void>(`/metrics-maps/${id}/share/`, { method: "DELETE" }),
    publicGet: (token: string) => request<PublicMetricsMap>(`/metrics-maps/public/${token}/`),
  },

  summary: () => request<SummaryResponse>("/summary/"),
  dashboard: () => request<DashboardResponse>("/dashboard/"),
  /** Workspace / dataset / table filter options. `workspace_name` narrows the
   *  returned datasets; `workspace_name` + `dataset_name` narrow the tables —
   *  so the three dropdowns can cascade (dependent filters). */
  filters: (params: { workspace_name?: string; dataset_name?: string } = {}) =>
    request<FiltersResponse>(`/filters/${qs(params)}`),
  pbCleanupCounts: (params: { workspace_name?: string; dataset_name?: string } = {}) =>
    request<Record<string, number>>(`/pb-cleanup-counts/${qs(params)}`),
  dbtInsights: (params: { section?: string } = {}) =>
    request<Record<string, unknown>>(`/dbt-insights/${qs(params)}`),
  /** Aggregated Power BI usage (Champions, Report Usage pivot). */
  powerbiUsage: (params: UsageParams = {}) =>
    request<UsageResponse>(`/powerbi-usage/${qs(params as Record<string, string | number | undefined>)}`),

  tasks: {
    list: (params: Record<string, string | number | undefined> = {}) =>
      request<Paginated<GovernanceTask>>(`/tasks/${qs(params)}`),
    done: (id: number) =>
      request<{ status: string; id: number; state: string }>(`/tasks/${id}/done/`, {
        method: "POST",
      }),
  },

  dataPersons: {
    list: (params: { is_owner?: boolean; is_steward?: boolean; is_other?: boolean } = {}) =>
      request<Paginated<DataPerson> | DataPerson[]>(`/data-persons/${qs(params)}`),
  },

  governance: {
    /** GET download URL for the governance CSV export (link directly). */
    exportCsvUrl: `${BASE}/governance/export-csv/`,
    /** Upload an edited governance CSV (multipart). */
    importCsv: async (file: File): Promise<GovernanceImportResult> => {
      const fd = new FormData();
      fd.append("file", file);
      const csrf = getCookie("csrftoken");
      const res = await fetch(`${BASE}/governance/import-csv/`, {
        method: "POST",
        headers: csrf ? { "X-CSRFToken": csrf } : {},
        body: fd,
        credentials: "include",
        cache: "no-store",
      });
      const data = (await res.json().catch(() => ({}))) as GovernanceImportResult;
      if (!res.ok) throw new ApiError(res.status, data.error ?? res.statusText, data);
      return data;
    },
  },

  org: {
    members: () => request<OrgMembersResponse>("/org/members/"),
    saveMember: (body: MemberInput) =>
      request<{ status: string; user_id: number }>("/org/members/save/", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    removeMember: (userId: number) =>
      request<{ status: string }>("/org/members/remove/", {
        method: "POST",
        body: JSON.stringify({ user_id: userId }),
      }),
    saveSettings: (body: OrgSettings) =>
      request<{ status: string }>("/org/settings/", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    /** Available PowerBI workspaces + BigQuery datasets for the assistant
     *  context-scope selectors. */
    assistantScope: () => request<AssistantScopeResponse>("/org/assistant-scope/"),
    queues: () => request<QueuesResponse>("/org/queues/"),
    /** Terminate a queued/running task by its OrmQ row id. Removes the broker
     *  row and, for a running known task, signals cooperative cancellation.
     *  `signalled` is the task family stopped ("source" | "destination" |
     *  "workflow") or null when the row was only dropped from the queue. */
    killQueued: (id: number) =>
      request<{ status: string; signalled: string | null }>(`/org/queues/${id}/kill/`, {
        method: "POST",
      }),
  },

  integrations: {
    getAll: () => request<IntegrationsData>("/integrations/"),

    // sources
    saveSource: (body: SourceInput) =>
      request<{ status: string; id: number }>("/integrations/sources/save/", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    runSource: (id: number) =>
      request<RunQueued>(`/integrations/sources/${id}/run/`, { method: "POST" }),
    testSource: (id: number) =>
      request<TestResult>(`/integrations/sources/${id}/test/`, { method: "POST" }),
    sourceLogs: (id: number) => request<RunLog[]>(`/integrations/sources/${id}/logs/`),
    logDetail: (logId: number) => request<RunLogDetail>(`/integrations/logs/${logId}/`),

    // destinations
    saveDestination: (body: DestinationInput) =>
      request<{ status: string; id: number; bq_project_id: string }>(
        "/integrations/destinations/save/",
        { method: "POST", body: JSON.stringify(body) },
      ),
    runDestination: (id: number) =>
      request<RunQueued>(`/integrations/destinations/${id}/run/`, { method: "POST" }),
    testDestination: (id: number) =>
      request<TestResult>(`/integrations/destinations/${id}/test/`, { method: "POST" }),
    destLogs: (id: number) =>
      request<RunLog[]>(`/integrations/destinations/${id}/logs/`),
    destLogDetail: (logId: number) =>
      request<RunLogDetail>(`/integrations/destinations/logs/${logId}/`),

    // run-history kill/delete (source + destination logs)
    // (kill endpoints are POST, delete endpoints are DELETE on the backend)
    killRun: (logId: number) =>
      request<{ status: string }>(`/integrations/logs/${logId}/kill/`, { method: "POST" }),
    deleteRun: (logId: number) =>
      request<{ status: string }>(`/integrations/logs/${logId}/delete/`, { method: "DELETE" }),
    killDestRun: (logId: number) =>
      request<{ status: string }>(`/integrations/destinations/logs/${logId}/kill/`, {
        method: "POST",
      }),
    deleteDestRun: (logId: number) =>
      request<{ status: string }>(`/integrations/destinations/logs/${logId}/delete/`, {
        method: "DELETE",
      }),

    // notifications (Slack hooks)
    saveHook: (body: {
      id?: number;
      name?: string;
      is_active?: boolean;
      slack_bot_token?: string;
      slack_channel?: string;
      slack_alerts_channel?: string;
      disconnect?: boolean;
    }) =>
      request<{ status: string; id: number }>("/integrations/hooks/save/", {
        method: "POST",
        body: JSON.stringify(body),
      }),

    // delete every run log for the org
    cleanLogs: () =>
      request<{ status: string; deleted?: Record<string, number> }>("/integrations/clean-logs/", {
        method: "POST",
      }),
  },

  workflow: {
    status: () => request<WorkflowStatus>("/integrations/workflow/"),
    run: () =>
      request<{ status: string; workflow_run_id?: number; task_id?: string; error?: string }>(
        "/integrations/workflow/run/",
        { method: "POST" },
      ),
    runDetail: (id: number) =>
      request<RunLogDetail>(`/integrations/workflow/${id}/`),
    saveSchedule: (body: WorkflowScheduleInput) =>
      request<{ status: string }>("/integrations/workflow/schedule/", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    toggleStep: (type: "source" | "destination", id: number, isActive: boolean) =>
      request<{ status: string; is_active: boolean }>("/integrations/workflow/toggle/", {
        method: "POST",
        body: JSON.stringify({ type, id, is_active: isActive }),
      }),

    // workflow-run kill/delete (kill is POST, delete is DELETE on the backend)
    killRun: (id: number) =>
      request<{ status: string }>(`/integrations/workflow/${id}/kill/`, { method: "POST" }),
    deleteRun: (id: number) =>
      request<{ status: string }>(`/integrations/workflow/${id}/delete/`, { method: "DELETE" }),

    // raw export → Google Cloud Storage (advanced workflow settings)
    saveRawExport: (body: {
      is_active: boolean;
      gcs_bucket_name: string;
      gcs_service_account_json?: string;
    }) =>
      request<{
        status: string;
        is_active: boolean;
        gcs_bucket_name: string;
        gcs_service_account_set: boolean;
      }>("/integrations/workflow/raw-export/", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    testRawExport: (body: { gcs_bucket_name?: string; gcs_service_account_json?: string }) =>
      request<TestResult>("/integrations/workflow/raw-export/test/", {
        method: "POST",
        body: JSON.stringify(body),
      }),
  },

  powerbiUsageData: () => request<Record<string, unknown>>("/powerbi-usage/"),

  chat: {
    send: (message: string, sessionId: number | null) =>
      request<{ session_id?: number; task_id?: string; error?: string }>("/chat/", {
        method: "POST",
        body: JSON.stringify({ message, session_id: sessionId }),
      }),
    taskStatus: (taskId: string) =>
      request<{ status: string; current_status?: string }>(`/chat/task/${taskId}/`),
    messages: (sessionId: number) =>
      request<{ role: string; content: string }[]>(`/chat/sessions/${sessionId}/messages/`),
    sessions: () =>
      request<{ id: number; title: string; updated_at: string }[]>("/chat/sessions/"),
    delete: (sessionId: number) =>
      request<void>(`/chat/sessions/${sessionId}/`, { method: "DELETE" }),
  },
};
