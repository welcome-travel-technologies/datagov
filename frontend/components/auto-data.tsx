"use client";

import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Stat } from "@/components/ui/misc";
import { Table, THead, TBody, TR, TH, TD } from "@/components/ui/table";
import { fmtInt } from "@/lib/utils";

/**
 * Best-effort renderer for arbitrary API JSON whose schema we don't model:
 * numbers become KPI stats, arrays-of-objects become tables, primitive arrays
 * become chips, nested objects recurse (bounded depth). Used for the
 * Integrations / Org pages so they show real backend data without a hand-built
 * form for every field.
 */
function humanize(k: string) {
  return k.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function isScalar(v: unknown): v is string | number | boolean {
  return v === null || ["string", "number", "boolean"].includes(typeof v);
}

function scalarText(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") return fmtInt(v);
  if (typeof v === "boolean") return v ? "Yes" : "No";
  return String(v);
}

function ObjectTable({ rows }: { rows: Record<string, unknown>[] }) {
  const keys = Array.from(
    rows.reduce<Set<string>>((set, r) => {
      Object.entries(r).forEach(([k, v]) => {
        if (isScalar(v)) set.add(k);
      });
      return set;
    }, new Set()),
  ).slice(0, 8);
  if (keys.length === 0) return null;
  return (
    <Table>
      <THead>
        <TR>{keys.map((k) => <TH key={k}>{humanize(k)}</TH>)}</TR>
      </THead>
      <TBody>
        {rows.map((r, i) => (
          <TR key={(r.id as string) ?? i}>
            {keys.map((k) => (
              <TD key={k} className="text-[13px]">
                {typeof r[k] === "boolean" ? (
                  <Badge variant={r[k] ? "success" : "default"}>{scalarText(r[k])}</Badge>
                ) : (
                  scalarText(r[k])
                )}
              </TD>
            ))}
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

export function AutoData({ data, depth = 0 }: { data: unknown; depth?: number }) {
  if (data === null || data === undefined) return <p className="text-[13px] text-muted-foreground">No data.</p>;

  if (Array.isArray(data)) {
    if (data.length === 0) return <p className="text-[13px] text-muted-foreground">Empty.</p>;
    if (data.every((d) => d && typeof d === "object" && !Array.isArray(d))) {
      return (
        <Card className="overflow-hidden">
          <ObjectTable rows={data as Record<string, unknown>[]} />
        </Card>
      );
    }
    return (
      <div className="flex flex-wrap gap-1.5">
        {data.map((d, i) => (
          <Badge key={i} variant="outline">
            {scalarText(d)}
          </Badge>
        ))}
      </div>
    );
  }

  if (typeof data === "object") {
    const entries = Object.entries(data as Record<string, unknown>);
    const numbers = entries.filter(([, v]) => typeof v === "number");
    const scalars = entries.filter(([, v]) => isScalar(v) && typeof v !== "number");
    const complex = entries.filter(([, v]) => !isScalar(v));

    return (
      <div className="space-y-5">
        {numbers.length > 0 && (
          <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
            {numbers.map(([k, v]) => (
              <Stat key={k} label={humanize(k)} value={fmtInt(v as number)} />
            ))}
          </div>
        )}
        {scalars.length > 0 && (
          <Card className="p-4">
            <dl className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {scalars.map(([k, v]) => (
                <div key={k} className="flex justify-between gap-3 text-[13px]">
                  <dt className="text-muted-foreground">{humanize(k)}</dt>
                  <dd className="text-right font-medium">{scalarText(v)}</dd>
                </div>
              ))}
            </dl>
          </Card>
        )}
        {depth < 2 &&
          complex.map(([k, v]) => (
            <section key={k}>
              <h3 className="mb-2 text-[12px] font-semibold uppercase tracking-[0.06em] text-faint">{humanize(k)}</h3>
              <AutoData data={v} depth={depth + 1} />
            </section>
          ))}
      </div>
    );
  }

  return <p className="text-[13px]">{scalarText(data)}</p>;
}
