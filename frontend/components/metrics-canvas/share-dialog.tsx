"use client";

import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Check, Copy, Link2, RefreshCw } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Spinner } from "@/components/ui/misc";
import { api } from "@/lib/api";

/** Public-viewer URL for a share token (absolute, so it can be copied/pasted). */
function shareUrl(token: string): string {
  const origin = typeof window !== "undefined" ? window.location.origin : "";
  return `${origin}/share/metrics-map/${token}`;
}

export interface ShareDialogProps {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  /** The saved map's id, or null when the current canvas hasn't been saved yet. */
  mapId: number | null;
  /** Current share token (null = not shared) — owned by the parent canvas state. */
  publicToken: string | null;
  /** Whether anonymous viewers may drag nodes. */
  canDrag: boolean;
  /** Save the current canvas first (used when there's no map id yet). */
  onSaveFirst: () => void;
  /** Push share-state changes back up so the parent's draft stays in sync. */
  onChanged: (patch: { public_token?: string | null; public_can_drag?: boolean }) => void;
}

/**
 * "Share this map" dialog. Turns a saved map into a public, read-only link
 * (unguessable uuid4 URL) that anyone — no login, any org — can open. Viewers
 * can drag nodes around (if allowed) but can't edit or save; their moves are
 * never persisted. The owner can copy the link, toggle viewer-drag, regenerate
 * the link, or stop sharing entirely.
 */
export function ShareDialog({
  open,
  onOpenChange,
  mapId,
  publicToken,
  canDrag,
  onSaveFirst,
  onChanged,
}: ShareDialogProps) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(false), 1600);
    return () => clearTimeout(t);
  }, [copied]);

  const shareMut = useMutation({
    mutationFn: (body: { can_drag?: boolean; rotate?: boolean }) =>
      api.metricsMaps.share(mapId as number, body),
    onSuccess: (res) =>
      onChanged({ public_token: res.public_token, public_can_drag: res.public_can_drag }),
  });

  const unshareMut = useMutation({
    mutationFn: () => api.metricsMaps.unshare(mapId as number),
    onSuccess: () => onChanged({ public_token: null }),
  });

  const busy = shareMut.isPending || unshareMut.isPending;
  const isShared = !!publicToken;

  function togglePublic(on: boolean) {
    if (!mapId || busy) return;
    if (on) shareMut.mutate({ can_drag: canDrag });
    else unshareMut.mutate();
  }

  async function copyLink() {
    if (!publicToken) return;
    try {
      await navigator.clipboard.writeText(shareUrl(publicToken));
      setCopied(true);
    } catch {
      /* clipboard blocked — no-op */
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Share this map</DialogTitle>
          <DialogDescription>
            Anyone with the link can open a read-only view of this map — no login required.
            They can rearrange items (if allowed) but can&apos;t edit or save.
          </DialogDescription>
        </DialogHeader>

        {mapId == null ? (
          // Sharing needs a persisted map (the token lives on the saved row).
          <div className="rounded-lg border border-line bg-panel/60 p-4 text-[13px]">
            <p className="text-muted-foreground">
              Save this map first to create a shareable link.
            </p>
            <Button variant="brand" size="sm" className="mt-3" onClick={onSaveFirst}>
              Save map
            </Button>
          </div>
        ) : (
          <div className="space-y-4">
            <label className="flex items-center justify-between gap-4">
              <span className="min-w-0">
                <span className="block text-[13px] font-medium">Anyone with the link can view</span>
                <span className="block text-[12px] text-muted-foreground">
                  Publish this map to a public, read-only URL.
                </span>
              </span>
              <Switch checked={isShared} disabled={busy} onCheckedChange={togglePublic} />
            </label>

            {isShared && (
              <>
                <div className="flex items-center gap-2 rounded-lg border border-line bg-panel/60 p-2">
                  <Link2 className="h-4 w-4 shrink-0 text-faint" />
                  <input
                    readOnly
                    value={shareUrl(publicToken!)}
                    onFocus={(e) => e.currentTarget.select()}
                    className="min-w-0 flex-1 bg-transparent text-[12.5px] text-foreground outline-none"
                  />
                  <Button variant="outline" size="sm" onClick={copyLink}>
                    {copied ? <Check className="text-brand" /> : <Copy />}
                    {copied ? "Copied" : "Copy"}
                  </Button>
                </div>

                <label className="flex items-center justify-between gap-4">
                  <span className="min-w-0">
                    <span className="block text-[13px] font-medium">
                      Viewers can rearrange (drag) items
                    </span>
                    <span className="block text-[12px] text-muted-foreground">
                      Lets viewers move nodes locally. Changes are never saved.
                    </span>
                  </span>
                  <Switch
                    checked={canDrag}
                    disabled={busy}
                    onCheckedChange={(v) => shareMut.mutate({ can_drag: v })}
                  />
                </label>

                <div className="flex items-center justify-between gap-2 border-t border-line pt-3">
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => shareMut.mutate({ rotate: true })}
                    className="inline-flex items-center gap-1.5 text-[12.5px] text-muted-foreground hover:text-foreground disabled:opacity-50"
                  >
                    <RefreshCw className="h-3.5 w-3.5" /> Regenerate link
                  </button>
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={busy}
                    onClick={() => unshareMut.mutate()}
                    className="text-err hover:bg-err/10 hover:text-err"
                  >
                    Stop sharing
                  </Button>
                </div>
              </>
            )}

            {busy && (
              <div className="flex items-center gap-2 text-[12px] text-faint">
                <Spinner /> Updating…
              </div>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
