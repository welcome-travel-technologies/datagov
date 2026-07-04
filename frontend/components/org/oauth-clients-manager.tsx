"use client";

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plug, Plus, Trash2, Copy, Check, AlertTriangle, ShieldCheck } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Table, THead, TBody, TR, TH, TD } from "@/components/ui/table";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Field, FormError } from "@/components/ui/form-field";
import { Input } from "@/components/ui/input";
import { LoadingState, EmptyState } from "@/components/ui/misc";
import { cn } from "@/lib/utils";
import {
  api,
  getApiErrorMessage,
  type OAuthClient,
  type OAuthClientsResponse,
  type OAuthClientCreated,
} from "@/lib/api";

const QK = ["org-oauth-clients"];

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function OAuthClientsManager() {
  const qc = useQueryClient();
  const { data, isLoading, isError } = useQuery({ queryKey: QK, queryFn: api.org.oauthClients });

  const [dialogOpen, setDialogOpen] = useState(false);

  const revokeMut = useMutation({
    mutationFn: (id: number) => api.org.revokeOauthClient(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: QK }),
  });

  if (isLoading) return <LoadingState label="Loading connectors…" />;
  if (isError || !data) {
    return (
      <Card>
        <div className="p-6">
          <EmptyState
            title="Couldn't load connectors"
            hint="This area requires Organization Settings (Admin) access."
          />
        </div>
      </Card>
    );
  }

  function revoke(c: OAuthClient) {
    if (
      window.confirm(
        `Delete connector "${c.name}"? Any client using it (e.g. claude.ai) stops working immediately. This can't be undone.`,
      )
    ) {
      revokeMut.mutate(c.id);
    }
  }

  return (
    <>
      <div className="mb-3 flex items-start justify-between gap-4">
        <p className="max-w-2xl text-[13px] text-muted-foreground">
          Connectors are OAuth clients that let a browser assistant —{" "}
          <span className="font-medium text-foreground">claude.ai&apos;s custom connector</span>{" "}
          — sign in to this catalog&apos;s MCP server. Create one, then paste its Client ID and
          Secret into claude.ai&apos;s connector dialog under{" "}
          <span className="font-medium text-foreground">Advanced settings</span>. Only org admins
          can approve the sign-in. For Claude Desktop / Code, use an{" "}
          <span className="font-medium text-foreground">MCP Key</span> instead.
        </p>
        <Button variant="brand" onClick={() => setDialogOpen(true)}>
          <Plus /> New connector
        </Button>
      </div>

      <div className="mb-3 flex flex-wrap items-center gap-2 text-[12.5px] text-muted-foreground">
        <span className="font-medium text-foreground">MCP endpoint URL:</span>
        <code className="rounded bg-panel2 px-2 py-0.5 font-mono text-[12px]">{data.endpoint}</code>
        <CopyButton text={data.endpoint} label="Copy" />
      </div>

      {revokeMut.isError && (
        <div className="mb-3 flex items-center gap-2 rounded-md bg-err/10 px-3 py-2 text-[12.5px] text-err">
          <AlertTriangle className="h-3.5 w-3.5" />
          {getApiErrorMessage(revokeMut.error, "Could not delete connector.")}
        </div>
      )}

      <Card className="overflow-hidden">
        {data.clients.length === 0 ? (
          <EmptyState
            title="No connectors yet"
            hint="Create one to connect claude.ai to this catalog."
          />
        ) : (
          <Table>
            <THead>
              <TR>
                <TH>Name</TH>
                <TH>Client ID</TH>
                <TH>Redirect URIs</TH>
                <TH>Created</TH>
                <TH className="text-right">Actions</TH>
              </TR>
            </THead>
            <TBody>
              {data.clients.map((c) => (
                <TR key={c.id}>
                  <TD>
                    <div className="font-medium">{c.name}</div>
                  </TD>
                  <TD>
                    <code className="font-mono text-[12px] text-muted-foreground">
                      {c.client_id}
                    </code>
                  </TD>
                  <TD>
                    <div className="flex flex-col gap-0.5">
                      {c.redirect_uris.map((u) => (
                        <span key={u} className="font-mono text-[11.5px] text-muted-foreground">
                          {u}
                        </span>
                      ))}
                    </div>
                  </TD>
                  <TD>
                    <span className="text-[12.5px] text-muted-foreground">
                      {fmtDate(c.created_at)}
                    </span>
                  </TD>
                  <TD className="text-right">
                    <Button
                      size="sm"
                      variant="ghost"
                      className="text-err hover:bg-err/10 hover:text-err"
                      disabled={revokeMut.isPending}
                      onClick={() => revoke(c)}
                    >
                      <Trash2 /> Delete
                    </Button>
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </Card>

      <CreateClientDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        data={data}
        onCreated={() => qc.invalidateQueries({ queryKey: QK })}
      />
    </>
  );
}

function CreateClientDialog({
  open,
  onOpenChange,
  data,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  data: OAuthClientsResponse;
  onCreated: () => void;
}) {
  const [name, setName] = useState("");
  const [redirectUri, setRedirectUri] = useState(data.default_redirect_uri);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<OAuthClientCreated | null>(null);

  useEffect(() => {
    if (open) {
      setName("");
      setRedirectUri(data.default_redirect_uri);
      setError(null);
      setCreated(null);
    }
  }, [open, data.default_redirect_uri]);

  const createMut = useMutation({
    mutationFn: () =>
      api.org.createOauthClient({
        name: name.trim(),
        redirect_uris: redirectUri
          .split(/[\s,]+/)
          .map((u) => u.trim())
          .filter(Boolean),
      }),
    onSuccess: (res) => {
      setCreated(res);
      onCreated();
    },
    onError: (e) => setError(getApiErrorMessage(e, "Could not create connector.")),
  });

  function submit() {
    if (!name.trim()) {
      setError("A connector name is required.");
      return;
    }
    setError(null);
    createMut.mutate();
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        {created ? (
          <CredentialsReveal created={created} endpoint={data.endpoint} onDone={() => onOpenChange(false)} />
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>New MCP connector</DialogTitle>
              <DialogDescription>
                The Client Secret is shown once, right after you create it — copy it then.
              </DialogDescription>
            </DialogHeader>

            <div className="space-y-4">
              <Field label="Name">
                <Input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="e.g. claude.ai"
                  autoFocus
                />
              </Field>

              <Field label="Redirect URI">
                <Input
                  value={redirectUri}
                  onChange={(e) => setRedirectUri(e.target.value)}
                  placeholder="https://claude.ai/api/mcp/auth_callback"
                />
                <p className="mt-1 text-[12px] text-muted-foreground">
                  Where the OAuth response is sent back. The default is claude.ai&apos;s callback —
                  leave it unless you know you need another. Must be https.
                </p>
              </Field>

              {error && <FormError>{error}</FormError>}
            </div>

            <div className="flex justify-end gap-2 pt-1">
              <Button variant="ghost" onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button variant="brand" onClick={submit} disabled={createMut.isPending}>
                <Plug />
                {createMut.isPending ? "Creating…" : "Create connector"}
              </Button>
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

function CredentialsReveal({
  created,
  endpoint,
  onDone,
}: {
  created: OAuthClientCreated;
  endpoint: string;
  onDone: () => void;
}) {
  return (
    <>
      <DialogHeader>
        <DialogTitle className="flex items-center gap-2">
          <ShieldCheck className="h-4 w-4 text-ok" />
          Connector created — copy the secret now
        </DialogTitle>
        <DialogDescription>
          This is the <span className="font-medium text-foreground">only</span> time the Client
          Secret is shown. Paste all three into claude.ai → Add custom connector (URL field +
          Advanced settings). If you lose the secret, delete this connector and create a new one.
        </DialogDescription>
      </DialogHeader>

      <div className="space-y-3">
        <RevealRow label="MCP endpoint URL (URL field)" value={endpoint} />
        <RevealRow label="OAuth Client ID" value={created.client_id} />
        <RevealRow label="OAuth Client Secret" value={created.client_secret} mono />
      </div>

      <div className="flex justify-end pt-1">
        <Button variant="brand" onClick={onDone}>
          <Check /> Done
        </Button>
      </div>
    </>
  );
}

function RevealRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <div className="mb-1 text-[12px] font-medium text-muted-foreground">{label}</div>
      <div className="flex items-center gap-2">
        <code
          className={cn(
            "min-w-0 flex-1 truncate rounded-md border border-line bg-panel2 px-3 py-2 text-[12.5px]",
            mono && "font-mono",
          )}
        >
          {value}
        </code>
        <CopyButton text={value} label="Copy" variant="outline" />
      </div>
    </div>
  );
}

function CopyButton({
  text,
  label,
  variant = "ghost",
}: {
  text: string;
  label: string;
  variant?: "ghost" | "outline";
}) {
  const [copied, setCopied] = useState(false);
  const timeoutMs = useMemo(() => 1500, []);
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), timeoutMs);
    } catch {
      /* clipboard may be blocked; the value is selectable in the box */
    }
  }
  return (
    <Button size="sm" variant={variant} onClick={copy}>
      {copied ? <Check className="text-ok" /> : <Copy />}
      {copied ? "Copied" : label}
    </Button>
  );
}
