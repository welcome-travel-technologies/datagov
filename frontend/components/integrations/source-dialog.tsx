"use client";

import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Save } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Field, FormError } from "@/components/ui/form-field";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from "@/components/ui/select";
import { ScheduleFields } from "@/components/integrations/schedule-fields";
import {
  scheduleStateFrom,
  schedulePayload,
  type ScheduleState,
} from "@/lib/integrations/schedule";
import { api, getApiErrorMessage, type IntegrationSource, type SourceInput } from "@/lib/api";

// Radix Select disallows empty-string item values, so use a sentinel for "none".
const NONE_WS = "__none__";

export function SourceDialog({
  open,
  onOpenChange,
  source,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  source: IntegrationSource;
  onSaved: () => void;
}) {
  const isDbt = source.source_type === "dbt";

  // Normalised dropdown options from the source's discovered workspaces, with
  // the currently-saved default appended if it isn't already in the list.
  const availableWorkspaces = (() => {
    const list = (source.available_workspaces || [])
      .filter((w): w is { id: string; name?: string } => Boolean(w.id))
      .map((w) => ({ id: w.id, name: w.name }));
    if (source.default_workspace_id && !list.some((w) => w.id === source.default_workspace_id)) {
      list.push({ id: source.default_workspace_id, name: source.default_workspace_id });
    }
    return list;
  })();

  const [name, setName] = useState(source.name);
  const [tenantId, setTenantId] = useState(source.tenant_id);
  const [clientId, setClientId] = useState(source.client_id);
  const [clientSecret, setClientSecret] = useState("");
  const [workspaceIds, setWorkspaceIds] = useState((source.workspace_ids || []).join(", "));
  const [defaultWs, setDefaultWs] = useState(source.default_workspace_id);
  const [repoUrl, setRepoUrl] = useState(source.github_repo_url);
  const [githubToken, setGithubToken] = useState("");
  const [branch, setBranch] = useState(source.github_branch);
  const [manifestPath, setManifestPath] = useState(source.dbt_manifest_path);
  const [schedule, setSchedule] = useState<ScheduleState>(scheduleStateFrom(source.schedule));
  const [error, setError] = useState<string | null>(null);

  // Re-seed from the latest source whenever the dialog opens.
  useEffect(() => {
    if (!open) return;
    setName(source.name);
    setTenantId(source.tenant_id);
    setClientId(source.client_id);
    setClientSecret("");
    setWorkspaceIds((source.workspace_ids || []).join(", "));
    setDefaultWs(source.default_workspace_id);
    setRepoUrl(source.github_repo_url);
    setGithubToken("");
    setBranch(source.github_branch);
    setManifestPath(source.dbt_manifest_path);
    setSchedule(scheduleStateFrom(source.schedule));
    setError(null);
  }, [open, source]);

  const saveMut = useMutation({
    mutationFn: () => {
      const body: SourceInput = {
        id: source.id,
        name,
        source_type: source.source_type,
        ...schedulePayload(schedule),
      };
      if (isDbt) {
        body.github_repo_url = repoUrl;
        body.github_branch = branch;
        body.dbt_manifest_path = manifestPath;
        if (githubToken) body.github_token = githubToken;
      } else {
        body.tenant_id = tenantId;
        body.client_id = clientId;
        body.workspace_ids = workspaceIds;
        body.default_workspace_id = defaultWs;
        if (clientSecret) body.client_secret = clientSecret;
      }
      return api.integrations.saveSource(body);
    },
    onSuccess: () => {
      onSaved();
      onOpenChange(false);
    },
    onError: (e) => setError(getApiErrorMessage(e, "Could not save source.")),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Configure {source.name}</DialogTitle>
          <DialogDescription>
            {isDbt
              ? "GitHub repository and dbt manifest settings used to extract dbt lineage."
              : "Azure AD credentials and workspaces used to extract PowerBI / Fabric metadata."}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <Field label="Display name">
            <Input value={name} onChange={(e) => setName(e.target.value)} />
          </Field>

          {isDbt ? (
            <>
              <Field label="GitHub repo URL">
                <Input
                  value={repoUrl}
                  onChange={(e) => setRepoUrl(e.target.value)}
                  placeholder="https://github.com/org/repo"
                />
              </Field>
              <Field
                label={source.github_token_set ? "GitHub token (set — blank keeps it)" : "GitHub token"}
              >
                <Input
                  type="password"
                  value={githubToken}
                  onChange={(e) => setGithubToken(e.target.value)}
                  placeholder={source.github_token_set ? "••••••••" : "ghp_…"}
                />
              </Field>
              <div className="grid grid-cols-2 gap-3">
                <Field label="Branch">
                  <Input value={branch} onChange={(e) => setBranch(e.target.value)} placeholder="main" />
                </Field>
                <Field label="Manifest path">
                  <Input
                    value={manifestPath}
                    onChange={(e) => setManifestPath(e.target.value)}
                    placeholder="target/manifest.json"
                  />
                </Field>
              </div>
            </>
          ) : (
            <>
              <div className="grid grid-cols-2 gap-3">
                <Field label="Tenant ID">
                  <Input value={tenantId} onChange={(e) => setTenantId(e.target.value)} />
                </Field>
                <Field label="Client ID">
                  <Input value={clientId} onChange={(e) => setClientId(e.target.value)} />
                </Field>
              </div>
              <Field
                label={source.client_secret_set ? "Client secret (set — blank keeps it)" : "Client secret"}
              >
                <Input
                  type="password"
                  value={clientSecret}
                  onChange={(e) => setClientSecret(e.target.value)}
                  placeholder={source.client_secret_set ? "••••••••" : "Secret value"}
                />
              </Field>
              <Field label="Workspace IDs (comma-separated)">
                <Input
                  value={workspaceIds}
                  onChange={(e) => setWorkspaceIds(e.target.value)}
                  placeholder="ws-id-1, ws-id-2"
                />
              </Field>
              <Field label="Default workspace (optional)">
                {availableWorkspaces.length > 0 ? (
                  <Select value={defaultWs || NONE_WS} onValueChange={(v) => setDefaultWs(v === NONE_WS ? "" : v)}>
                    <SelectTrigger>
                      <SelectValue placeholder="No default" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={NONE_WS}>No default</SelectItem>
                      {availableWorkspaces.map((w) => (
                        <SelectItem key={w.id} value={w.id}>
                          {w.name || w.id}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                ) : (
                  <>
                    <Input
                      value={defaultWs}
                      onChange={(e) => setDefaultWs(e.target.value)}
                      placeholder="Workspace ID"
                    />
                    <span className="text-[11.5px] text-muted-foreground">
                      Run the source once to populate workspaces.
                    </span>
                  </>
                )}
              </Field>
            </>
          )}

          <ScheduleFields value={schedule} onChange={setSchedule} />

          {error && <FormError>{error}</FormError>}
        </div>

        <div className="flex justify-end gap-2 pt-1">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="brand" onClick={() => saveMut.mutate()} disabled={saveMut.isPending}>
            <Save /> {saveMut.isPending ? "Saving…" : "Save"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
