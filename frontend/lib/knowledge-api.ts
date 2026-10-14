import { request } from "@/lib/api";

export interface DocumentCitation {
  citation_id?: string;
  document_id: string;
  version_id: string;
  chunk_id?: string;
  title: string;
  page?: number | null;
  excerpt?: string;
  source_type?: string;
  source_url?: string | null;
  datagov_url?: string;
}

export interface KnowledgeSource {
  id: string;
  name: string;
  kind: "upload" | "snapshot";
  is_active: boolean;
  cron: string;
  timezone: string;
  last_run_at: string | null;
  config: Record<string, unknown>;
  access: "organization" | "restricted";
  allowed_user_ids: number[];
  allowed_group_ids: number[];
  integration_id?: number | null;
  import_group?: string;
  refresh_paused?: boolean;
  pipeline_enabled?: boolean;
}

export interface ContentGroup {
  id: string; name: string; provider: string; document_count: number;
  collections: { id: string; name: string; kind: string; document_count: number; is_active: boolean; refresh_paused: boolean; pipeline_enabled: boolean; last_run_at: string | null }[];
}

export interface KnowledgeDocument {
  id: string;
  title: string;
  mime_type: string;
  source_type: string;
  source_name?: string;
  source_url?: string | null;
  source_id?: string;
  status: string;
  current_version_id: string | null;
  updated_at: string;
  created_at: string;
}

export interface DocumentDetail extends KnowledgeDocument {
  version_id: string | null;
  pages: { page: number; text: string }[];
  chunks: { id: string; page: number | null; text: string; section?: string }[];
  content_url: string | null;
  versions: { id: string; created_at: string; status: string }[];
  datagov_url: string;
  error?: string;
}

export interface KnowledgeRun {
  id: string;
  source_id: string;
  source_name: string;
  status: string;
  stage: string;
  progress: number;
  counters: Record<string, number>;
  error: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  cancel_requested: boolean;
  upstream_key?: string | null;
  profile?: string | null;
}

export type SourceInput = Pick<KnowledgeSource, "name" | "kind" | "is_active" | "cron" | "timezone" | "config" | "access" | "allowed_user_ids" | "allowed_group_ids" | "integration_id" | "import_group" | "refresh_paused" | "pipeline_enabled">;

function query(values: Record<string, string | number | undefined>) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== "") params.set(key, String(value));
  }
  return params.size ? `?${params.toString()}` : "";
}

/** Always build links from identifiers; never navigate to a model-provided URL. */
export function documentHref(document: { document_id: string; version_id?: string | null; page?: number | null; chunk_id?: string }) {
  return `/knowledge/documents/${encodeURIComponent(document.document_id)}${query({
    version: document.version_id || undefined,
    page: document.page && document.page > 0 ? document.page : undefined,
  })}${document.chunk_id ? `#chunk-${encodeURIComponent(document.chunk_id)}` : ""}`;
}

export function documentContentHref(documentId: string, versionId?: string | null) {
  return `/api/knowledge/documents/${encodeURIComponent(documentId)}/content/${query({ version: versionId || undefined })}`;
}

/** Provider URLs are optional navigation, never the document's canonical link. */
export function safeSourceUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

export const knowledgeApi = {
  status: () => request<{ enabled: boolean; configured: boolean; can_manage: boolean; native_ai_enabled?: boolean }>("/knowledge/status/"),
  groups: () => request<{ results: ContentGroup[]; can_manage: boolean }>("/knowledge/groups/"),
  connections: () => request<{ results: { id: number; name: string; provider: string; capabilities: string[] }[] }>("/knowledge/connections/"),
  indexingOutputs: () => request<{ results: { id: string; source_name: string; upstream_key: string; profile: string; status: string; error: string; created_at: string; run_id: string | null }[] }>("/knowledge/indexing-outputs/"),
  sources: () => request<{ results: KnowledgeSource[] }>("/knowledge/sources/"),
  createSource: (body: SourceInput) => request<KnowledgeSource>("/knowledge/sources/", { method: "POST", body: JSON.stringify(body) }),
  updateSource: (id: string, body: Partial<SourceInput>) => request<KnowledgeSource>(`/knowledge/sources/${encodeURIComponent(id)}/`, { method: "PATCH", body: JSON.stringify(body) }),
  syncSource: (id: string) => request<KnowledgeRun>(`/knowledge/sources/${encodeURIComponent(id)}/sync/`, { method: "POST", body: "{}" }),
  documents: (params: { search?: string; page?: number; page_size?: number; source_id?: string; status?: string; group?: string } = {}) =>
    request<{ count: number; results: KnowledgeDocument[] }>(`/knowledge/documents/${query(params)}`),
  document: (id: string, version?: string | null) => request<DocumentDetail>(`/knowledge/documents/${encodeURIComponent(id)}/${query({ version: version || undefined })}`),
  upload: (file: File, sourceId: string, documentId?: string) => {
    const body = new FormData();
    body.append("file", file);
    body.append("source_id", sourceId);
    if (documentId) body.append("document_id", documentId);
    return request<{ document: KnowledgeDocument; run?: KnowledgeRun; unchanged?: boolean }>("/knowledge/uploads/", { method: "POST", body });
  },
  runs: () => request<{ results: KnowledgeRun[] }>("/knowledge/runs/"),
  cancelRun: (id: string) => request<KnowledgeRun>(`/knowledge/runs/${encodeURIComponent(id)}/cancel/`, { method: "POST", body: "{}" }),
  retryRun: (id: string) => request<KnowledgeRun>(`/knowledge/runs/${encodeURIComponent(id)}/retry/`, { method: "POST", body: "{}" }),
};
