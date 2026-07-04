"use client";

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Plus, Trash2, Copy, Check, AlertTriangle, ShieldCheck } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
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
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from "@/components/ui/select";
import { Field, FormError } from "@/components/ui/form-field";
import { Input } from "@/components/ui/input";
import { LoadingState, EmptyState } from "@/components/ui/misc";
import { cn } from "@/lib/utils";
import {
  api,
  getApiErrorMessage,
  type McpKey,
  type McpKeysResponse,
  type McpKeyCreated,
} from "@/lib/api";

const QK = ["org-mcp-keys"];

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  // YYYY-MM-DD HH:MM (local), stable and compact for a table cell.
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

export function McpKeysManager() {
  const qc = useQueryClient();
  const { data, isLoading, isError } = useQuery({ queryKey: QK, queryFn: api.org.mcpKeys });

  const [dialogOpen, setDialogOpen] = useState(false);

  const revokeMut = useMutation({
    mutationFn: (keyId: number) => api.org.revokeMcpKey(keyId),
    onSuccess: () => qc.invalidateQueries({ queryKey: QK }),
  });

  const deleteMut = useMutation({
    mutationFn: (keyId: number) => api.org.deleteMcpKey(keyId),
    onSuccess: () => qc.invalidateQueries({ queryKey: QK }),
  });

  if (isLoading) return <LoadingState label="Loading MCP keys…" />;
  if (isError || !data) {
    return (
      <Card>
        <CardContent className="pt-6">
          <EmptyState
            title="Couldn't load MCP keys"
            hint="This area requires Organization Settings (Admin) access."
          />
        </CardContent>
      </Card>
    );
  }

  function revoke(k: McpKey) {
    if (
      window.confirm(
        `Revoke "${k.name}"? Any MCP client using this key stops working immediately. This can't be undone.`,
      )
    ) {
      revokeMut.mutate(k.id);
    }
  }

  function del(k: McpKey) {
    if (
      window.confirm(
        `Permanently delete "${k.name}"? This removes the key row entirely. This can't be undone.`,
      )
    ) {
      deleteMut.mutate(k.id);
    }
  }

  return (
    <>
      <div className="mb-3 flex items-start justify-end gap-4">
        <Button variant="brand" onClick={() => setDialogOpen(true)}>
          <Plus /> New key
        </Button>
      </div>

      {revokeMut.isError && (
        <div className="mb-3 flex items-center gap-2 rounded-md bg-err/10 px-3 py-2 text-[12.5px] text-err">
          <AlertTriangle className="h-3.5 w-3.5" />
          {getApiErrorMessage(revokeMut.error, "Could not revoke key.")}
        </div>
      )}

      {deleteMut.isError && (
        <div className="mb-3 flex items-center gap-2 rounded-md bg-err/10 px-3 py-2 text-[12.5px] text-err">
          <AlertTriangle className="h-3.5 w-3.5" />
          {getApiErrorMessage(deleteMut.error, "Could not delete key.")}
        </div>
      )}

      <Card className="overflow-hidden">
        {data.keys.length === 0 ? (
          <EmptyState
            title="No MCP keys yet"
            hint="Create a key to connect an MCP client to this catalog."
          />
        ) : (
          <Table>
            <THead>
              <TR>
                <TH>Key</TH>
                <TH>Owner</TH>
                <TH>Scopes</TH>
                <TH>Last used</TH>
                <TH className="text-right">Actions</TH>
              </TR>
            </THead>
            <TBody>
              {data.keys.map((k) => (
                <TR key={k.id} className={cn(!k.is_active && "opacity-55")}>
                  <TD>
                    <div className="font-medium">{k.name}</div>
                    <div className="font-mono text-[12px] text-muted-foreground">
                      {k.key_prefix}…
                    </div>
                  </TD>
                  <TD>
                    <div className="flex items-center gap-1.5 text-[13px]">
                      {k.owner_display || k.owner_email || "—"}
                      {k.is_self && <span className="text-[11px] text-faint">(you)</span>}
                    </div>
                  </TD>
                  <TD>
                    <div className="flex flex-wrap gap-1">
                      {k.scopes.map((s) => (
                        <Badge key={s} variant="outline">
                          {s}
                        </Badge>
                      ))}
                    </div>
                  </TD>
                  <TD>
                    <span className="text-[12.5px] text-muted-foreground">
                      {fmtDate(k.last_used_at)}
                    </span>
                  </TD>
                  <TD className="text-right">
                    {k.is_active ? (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="text-err hover:bg-err/10 hover:text-err"
                        disabled={revokeMut.isPending}
                        onClick={() => revoke(k)}
                      >
                        <Trash2 /> Revoke
                      </Button>
                    ) : (
                      <div className="flex items-center justify-end gap-2">
                        <Badge variant="outline">Revoked</Badge>
                        <Button
                          size="sm"
                          variant="ghost"
                          className="text-err hover:bg-err/10 hover:text-err"
                          disabled={deleteMut.isPending}
                          onClick={() => del(k)}
                        >
                          <Trash2 /> Delete
                        </Button>
                      </div>
                    )}
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </Card>

      <CreateKeyDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        data={data}
        onCreated={() => qc.invalidateQueries({ queryKey: QK })}
      />
    </>
  );
}

const SELF = "self";

function CreateKeyDialog({
  open,
  onOpenChange,
  data,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  data: McpKeysResponse;
  onCreated: () => void;
}) {
  const [name, setName] = useState("");
  const [ownerId, setOwnerId] = useState<string>(SELF);
  const [scopes, setScopes] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<McpKeyCreated | null>(null);

  const self = useMemo(() => data.members.find((m) => m.is_self), [data.members]);

  // Reset each time the dialog opens (default to catalog:read checked).
  useEffect(() => {
    if (open) {
      setName("");
      setOwnerId(SELF);
      setScopes(data.available_scopes.filter((s) => s.value === "catalog:read").map((s) => s.value));
      setError(null);
      setCreated(null);
    }
  }, [open, data.available_scopes]);

  const createMut = useMutation({
    mutationFn: () =>
      api.org.createMcpKey({
        name: name.trim(),
        scopes,
        user_id: ownerId === SELF ? self?.user_id : Number(ownerId),
      }),
    onSuccess: (res) => {
      setCreated(res);
      onCreated();
    },
    onError: (e) => setError(getApiErrorMessage(e, "Could not create key.")),
  });

  function submit() {
    if (!name.trim()) {
      setError("A key name is required.");
      return;
    }
    if (scopes.length === 0) {
      setError("Select at least one scope.");
      return;
    }
    setError(null);
    createMut.mutate();
  }

  const toggleScope = (value: string) =>
    setScopes((cur) => (cur.includes(value) ? cur.filter((v) => v !== value) : [...cur, value]));

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        {created ? (
          <TokenReveal created={created} endpoint={data.endpoint} onDone={() => onOpenChange(false)} />
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>New MCP key</DialogTitle>
              <DialogDescription>
                The token is shown once, right after you create it — copy it then.
              </DialogDescription>
            </DialogHeader>

            <div className="space-y-4">
              <Field label="Name">
                <Input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="e.g. Claude Desktop — Jane"
                  autoFocus
                />
              </Field>

              <Field label="Acts as">
                <Select value={ownerId} onValueChange={setOwnerId}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={SELF}>
                      {self ? `${self.display_name} (you)` : "You"}
                    </SelectItem>
                    {data.members
                      .filter((m) => !m.is_self)
                      .map((m) => (
                        <SelectItem key={m.user_id} value={String(m.user_id)}>
                          {m.display_name}
                        </SelectItem>
                      ))}
                  </SelectContent>
                </Select>
                <p className="mt-1 text-[12px] text-muted-foreground">
                  The key inherits this member&apos;s data access.
                </p>
              </Field>

              <Field label="Scopes">
                <div className="space-y-1.5">
                  {data.available_scopes.map((s) => (
                    <label
                      key={s.value}
                      className="flex cursor-pointer items-start gap-2.5 rounded-lg border border-line bg-panel2/40 px-3 py-2 hover:bg-foreground/[0.03]"
                    >
                      <input
                        type="checkbox"
                        className="mt-0.5 h-3.5 w-3.5 accent-brand"
                        checked={scopes.includes(s.value)}
                        onChange={() => toggleScope(s.value)}
                      />
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 text-[13px] font-medium">
                          <span className="font-mono text-[12px]">{s.value}</span>
                          <span className="text-muted-foreground">· {s.label}</span>
                        </div>
                        <div className="text-[12px] text-muted-foreground">{s.hint}</div>
                        {!s.active && (
                          <div className="mt-0.5 flex items-center gap-1 text-[11.5px] text-warn">
                            <AlertTriangle className="h-3 w-3" />
                            Not enabled for this org yet — the key will mint but this scope has
                            no effect until the tool is turned on.
                          </div>
                        )}
                      </div>
                    </label>
                  ))}
                </div>
              </Field>

              {error && <FormError>{error}</FormError>}
            </div>

            <div className="flex justify-end gap-2 pt-1">
              <Button variant="ghost" onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button variant="brand" onClick={submit} disabled={createMut.isPending}>
                <KeyRound />
                {createMut.isPending ? "Creating…" : "Create key"}
              </Button>
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

function TokenReveal({
  created,
  endpoint,
  onDone,
}: {
  created: McpKeyCreated;
  endpoint: string;
  onDone: () => void;
}) {
  const [copied, setCopied] = useState<"token" | "config" | null>(null);

  const config = useMemo(
    () =>
      JSON.stringify(
        {
          mcpServers: {
            "welcome-data-catalog": {
              type: "http",
              url: endpoint,
              headers: { Authorization: `Bearer ${created.token}` },
            },
          },
        },
        null,
        2,
      ),
    [endpoint, created.token],
  );

  async function copy(text: string, which: "token" | "config") {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(which);
      window.setTimeout(() => setCopied(null), 1500);
    } catch {
      /* clipboard may be blocked; the value is selectable in the box */
    }
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle className="flex items-center gap-2">
          <ShieldCheck className="h-4 w-4 text-ok" />
          Key created — copy it now
        </DialogTitle>
        <DialogDescription>
          This is the <span className="font-medium text-foreground">only</span> time the full
          token is shown. If you lose it, revoke the key and create a new one.
        </DialogDescription>
      </DialogHeader>

      <div className="space-y-4">
        <div>
          <div className="mb-1 text-[12px] font-medium text-muted-foreground">Token</div>
          <div className="flex items-center gap-2">
            <code className="min-w-0 flex-1 truncate rounded-md border border-line bg-panel2 px-3 py-2 font-mono text-[12.5px]">
              {created.token}
            </code>
            <Button size="sm" variant="outline" onClick={() => copy(created.token, "token")}>
              {copied === "token" ? <Check className="text-ok" /> : <Copy />}
              {copied === "token" ? "Copied" : "Copy"}
            </Button>
          </div>
        </div>

        <div>
          <div className="mb-1 flex items-center justify-between">
            <div className="text-[12px] font-medium text-muted-foreground">
              MCP client config
            </div>
            <Button size="sm" variant="ghost" onClick={() => copy(config, "config")}>
              {copied === "config" ? <Check className="text-ok" /> : <Copy />}
              {copied === "config" ? "Copied" : "Copy JSON"}
            </Button>
          </div>
          <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-all rounded-md border border-line bg-panel2 p-3 font-mono text-[12px] leading-relaxed">
            {config}
          </pre>
        </div>
      </div>

      <div className="flex justify-end pt-1">
        <Button variant="brand" onClick={onDone}>
          <Check /> Done
        </Button>
      </div>
    </>
  );
}
