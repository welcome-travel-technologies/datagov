"use client";

import { useEffect, useRef, useState } from "react";
import { Search, X, Loader2 } from "lucide-react";
import { api, type NetworkNode } from "@/lib/api";
import { nodeDisplayLabel, GROUP_ORDER, GROUP_LABELS, colorFor } from "@/lib/lineage/graph-utils";
import { cn } from "@/lib/utils";

interface Props {
  value: string | null;
  label?: string | null;
  onPick: (id: string, node: NetworkNode | null) => void;
  groupFilter?: string;
  placeholder?: string;
  /** Add the "load entire graph" sentinel option. */
  allowAll?: boolean;
  disabled?: boolean;
  className?: string;
}

/** Async, grouped asset search (hits /api/network/?q=). Mirrors the Select2 box. */
export function SearchSelect({
  value,
  label,
  onPick,
  groupFilter = "",
  placeholder = "Type 2+ chars to search assets…",
  allowAll = false,
  disabled = false,
  className,
}: Props) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [results, setResults] = useState<NetworkNode[]>([]);
  const [loading, setLoading] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function onDoc(e: MouseEvent) {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  useEffect(() => {
    if (!open) return;
    const term = q.trim();
    if (term.length < 2) {
      setResults([]);
      return;
    }
    setLoading(true);
    const t = setTimeout(async () => {
      try {
        const data = await api.network.search(term, groupFilter);
        setResults(data.nodes || []);
      } catch {
        setResults([]);
      } finally {
        setLoading(false);
      }
    }, 300);
    return () => clearTimeout(t);
  }, [q, open, groupFilter]);

  // group results by node group, in GROUP_ORDER
  const grouped: Record<string, NetworkNode[]> = {};
  for (const n of results) (grouped[n.group || "OTHER"] ||= []).push(n);
  const orderedGroups = Object.keys(grouped).sort((a, b) => {
    const ia = GROUP_ORDER.indexOf(a);
    const ib = GROUP_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });

  const display = value ? label || value : "";

  return (
    <div ref={boxRef} className={cn("relative", className)}>
      <div
        className={cn(
          "flex h-9 items-center gap-2 rounded-md border border-input bg-panel px-3 text-[13px]",
          disabled && "cursor-not-allowed opacity-50",
        )}
        onClick={() => !disabled && setOpen(true)}
      >
        <Search className="h-3.5 w-3.5 shrink-0 text-faint" />
        {open ? (
          <input
            autoFocus
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={placeholder}
            className="min-w-0 flex-1 bg-transparent outline-none placeholder:text-faint"
          />
        ) : (
          <span className={cn("min-w-0 flex-1 truncate", !display && "text-faint")}>
            {display || placeholder}
          </span>
        )}
        {value && !open && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              onPick("", null);
            }}
            className="text-faint hover:text-foreground"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        )}
        {loading && open && <Loader2 className="h-3.5 w-3.5 animate-spin text-faint" />}
      </div>

      {open && (
        <div className="absolute z-30 mt-1 max-h-80 w-full min-w-[260px] overflow-auto rounded-md border border-line bg-panel p-1 shadow-lg">
          {allowAll && (
            <button
              onClick={() => {
                onPick("ALL", null);
                setOpen(false);
                setQ("");
              }}
              className="block w-full rounded-sm px-2 py-1.5 text-left text-[12px] font-semibold text-warn hover:bg-panel2"
            >
              ⚠ Load entire graph (slow)
            </button>
          )}
          {q.trim().length < 2 && (
            <div className="px-2 py-2 text-[12px] text-faint">Type 2+ characters to search…</div>
          )}
          {q.trim().length >= 2 && !loading && results.length === 0 && (
            <div className="px-2 py-2 text-[12px] text-faint">No matches.</div>
          )}
          {orderedGroups.map((g) => (
            <div key={g} className="mb-1">
              <div className="px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.06em] text-faint">
                {GROUP_LABELS[g] || g} ({grouped[g].length})
              </div>
              {grouped[g].map((n) => (
                <button
                  key={n.id}
                  onClick={() => {
                    onPick(n.id, n);
                    setOpen(false);
                    setQ("");
                  }}
                  className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-[12.5px] hover:bg-panel2"
                >
                  <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: colorFor(n.group) }} />
                  <span className="truncate">{nodeDisplayLabel(n)}</span>
                </button>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
