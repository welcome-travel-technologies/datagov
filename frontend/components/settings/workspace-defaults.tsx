"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Save } from "lucide-react";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from "@/components/ui/select";
import { LoadingState } from "@/components/ui/misc";
import { api, type WorkspaceSource } from "@/lib/api";

const NONE = "__none__";

/** Per-source default PowerBI workspace picker. The chatbot uses these to scope
 * queries (ported from the classic User Settings page). */
export function WorkspaceDefaults() {
  const query = useQuery({ queryKey: ["me-workspaces"], queryFn: api.me.workspaces });
  const sources = query.data?.sources ?? [];

  // Local selection map: { [sourceId]: workspaceId }.
  const [sel, setSel] = useState<Record<string, string>>({});
  useEffect(() => {
    const next: Record<string, string> = {};
    for (const s of sources) next[String(s.id)] = s.selected_id || "";
    setSel(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query.data]);

  const saveMut = useMutation({
    mutationFn: () => api.me.saveWorkspaces(sel),
  });

  if (query.isLoading) return <LoadingState label="Loading workspaces…" />;
  // Nothing to configure (no PowerBI sources with workspaces) — hide the card.
  if (!query.isError && sources.length === 0) return null;

  return (
    <Card className="md:col-span-2">
      <CardHeader>
        <CardTitle>Default workspaces</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-[13px] text-muted-foreground">
          Pick a default PowerBI workspace per source. The AI Assistant uses these to scope its
          queries when a source spans multiple workspaces.
        </p>

        <div className="space-y-3">
          {sources.map((s) => (
            <WorkspaceRow
              key={s.id}
              source={s}
              value={sel[String(s.id)] || ""}
              onChange={(v) => setSel((m) => ({ ...m, [String(s.id)]: v }))}
            />
          ))}
        </div>

        <div className="flex items-center gap-3">
          <Button variant="brand" size="sm" onClick={() => saveMut.mutate()} disabled={saveMut.isPending}>
            <Save /> {saveMut.isPending ? "Saving…" : "Save defaults"}
          </Button>
          {saveMut.isSuccess && <span className="text-[12.5px] text-ok">Saved.</span>}
          {saveMut.isError && <span className="text-[12.5px] text-err">Save failed.</span>}
        </div>
      </CardContent>
    </Card>
  );
}

function WorkspaceRow({
  source,
  value,
  onChange,
}: {
  source: WorkspaceSource;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-4 rounded-lg border border-line bg-panel2/40 px-3 py-2.5">
      <div className="min-w-0">
        <div className="truncate text-[13px] font-medium">{source.name}</div>
        <div className="text-[12px] text-muted-foreground">PowerBI / Fabric</div>
      </div>
      {source.auto_only ? (
        <Badge variant="outline">{source.workspaces[0]?.name ?? "Single workspace"}</Badge>
      ) : (
        <div className="w-56 shrink-0">
          <Select value={value || NONE} onValueChange={(v) => onChange(v === NONE ? "" : v)}>
            <SelectTrigger>
              <SelectValue placeholder="No default" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={NONE}>No default (ask each time)</SelectItem>
              {source.workspaces.map((w) => (
                <SelectItem key={w.id} value={w.id}>
                  {w.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      )}
    </div>
  );
}
