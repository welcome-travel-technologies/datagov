"use client";

import { useMemo } from "react";
import { cn } from "@/lib/utils";

/**
 * Tiny dependency-free code highlighter (SQL + YAML) with line numbers, used in
 * the item-detail Code / YAML viewer and the lineage model sidebar (mirrors
 * dbt-colibri's viewer). Colors come from theme-aware `--sql-*` OKLCH tokens so
 * it adapts to dark mode.
 */

export type CodeLang = "sql" | "yaml";

type Kind = "keyword" | "string" | "number" | "operator" | "comment" | "plain";

const SQL_KEYWORDS = new Set([
  "select", "from", "where", "with", "as", "and", "or", "not", "in", "is",
  "null", "distinct", "group", "by", "order", "having", "limit", "offset",
  "join", "left", "right", "inner", "outer", "full", "cross", "on", "using",
  "union", "all", "except", "intersect", "case", "when", "then", "else", "end",
  "over", "partition", "qualify", "window", "asc", "desc", "between", "like",
  "ilike", "exists", "cast", "lateral", "unnest", "create", "table", "view",
  "temporary", "temp", "insert", "into", "values", "update", "set", "delete",
  "true", "false", "filter", "rows", "range", "preceding", "following",
  "current", "row", "unbounded", "interval", "primary", "key", "references",
]);

const OP_CHARS = new Set("+-*/%=<>!|&^~".split(""));

// SQL: comments | strings | "quoted ids" | numbers | words | ws | char.
const SQL_RE =
  /(--[^\n]*|\/\*[\s\S]*?\*\/)|('(?:[^']|'')*'|`[^`]*`)|("(?:[^"]|"")*")|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_$]*)|(\s+)|([^\s])/g;

// YAML: comments | strings | keys (word before `: `) | numbers | bool/null | ws | char.
const YAML_RE =
  /(#[^\n]*)|('(?:[^']|'')*'|"(?:[^"\\]|\\.)*")|([A-Za-z_][\w.\-]*)(?=:\s)|(-?\b\d+(?:\.\d+)?\b)|(\b(?:true|false|null|yes|no|~)\b)|(\s+)|([^\s])/g;

type Seg = { text: string; kind: Kind };

function tokenizeSql(sql: string): Seg[] {
  const out: Seg[] = [];
  let m: RegExpExecArray | null;
  SQL_RE.lastIndex = 0;
  while ((m = SQL_RE.exec(sql)) !== null) {
    const [full, comment, str, dq, num, word, ws, ch] = m;
    if (comment) out.push({ text: full, kind: "comment" });
    else if (str || dq) out.push({ text: full, kind: "string" });
    else if (num) out.push({ text: full, kind: "number" });
    else if (word) out.push({ text: full, kind: SQL_KEYWORDS.has(word.toLowerCase()) ? "keyword" : "plain" });
    else if (ws) out.push({ text: full, kind: "plain" });
    else if (ch) out.push({ text: full, kind: OP_CHARS.has(ch) ? "operator" : "plain" });
  }
  return out;
}

function tokenizeYaml(src: string): Seg[] {
  const out: Seg[] = [];
  let m: RegExpExecArray | null;
  YAML_RE.lastIndex = 0;
  while ((m = YAML_RE.exec(src)) !== null) {
    const [full, comment, str, key, num, bool, ws, ch] = m;
    if (comment) out.push({ text: full, kind: "comment" });
    else if (str) out.push({ text: full, kind: "string" });
    else if (key) out.push({ text: full, kind: "keyword" });
    else if (num) out.push({ text: full, kind: "number" });
    else if (bool) out.push({ text: full, kind: "number" });
    else if (ws) out.push({ text: full, kind: "plain" });
    else if (ch) out.push({ text: full, kind: OP_CHARS.has(ch) ? "operator" : "plain" });
  }
  return out;
}

/** Split flat segments into per-line arrays so each line gets a gutter number. */
function toLines(segs: Seg[]): Seg[][] {
  const lines: Seg[][] = [[]];
  for (const seg of segs) {
    const parts = seg.text.split("\n");
    parts.forEach((part, i) => {
      if (i > 0) lines.push([]);
      if (part) lines[lines.length - 1].push({ text: part, kind: seg.kind });
    });
  }
  return lines;
}

const COLOR: Record<Kind, string | undefined> = {
  keyword: "oklch(var(--sql-keyword))",
  string: "oklch(var(--sql-string))",
  number: "oklch(var(--sql-number))",
  operator: "oklch(var(--sql-operator))",
  comment: "oklch(var(--sql-comment))",
  plain: undefined,
};

export function CodeBlock({
  code,
  language = "sql",
  className,
}: {
  code: string;
  language?: CodeLang;
  className?: string;
}) {
  const lines = useMemo(
    () => toLines(language === "yaml" ? tokenizeYaml(code) : tokenizeSql(code)),
    [code, language],
  );
  const gutterWidth = `${String(lines.length).length + 1}ch`;

  return (
    <pre
      className={cn(
        "min-w-max font-mono text-[11.5px] leading-[1.55] text-foreground/90",
        className,
      )}
    >
      <code>
        {lines.map((segs, i) => (
          <span key={i} className="block w-max">
            <span
              className="mr-3 inline-block select-none text-right italic text-faint/60"
              style={{ width: gutterWidth }}
            >
              {i + 1}
            </span>
            {segs.map((seg, j) => (
              <span
                key={j}
                style={seg.kind === "comment" ? { color: COLOR[seg.kind], fontStyle: "italic" } : { color: COLOR[seg.kind] }}
              >
                {seg.text}
              </span>
            ))}
          </span>
        ))}
      </code>
    </pre>
  );
}
