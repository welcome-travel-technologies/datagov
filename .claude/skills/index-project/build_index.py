#!/usr/bin/env python3
"""
build_index.py — deterministic, dependency-free codebase indexer.

Walks the git-tracked source files and extracts a structured symbol map
(Django models / views / serializers / endpoints / ETL / management commands,
Next.js routes / components / lib modules / hooks / API types) into a set of
grep-optimized index files under `.claude/index/`.

The point: searching the index (one flat TSV + a few markdown maps) is far
faster than re-scanning ~250 source files for "where does X live?".

Usage:
    python build_index.py            # full rebuild of .claude/index/
    python build_index.py --check    # report whether the index is stale, exit 1 if so

No third-party dependencies. Python 3.8+.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import defaultdict

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

# Paths (relative to repo root) excluded from indexing entirely.
EXCLUDE_DIR_PARTS = (
    "node_modules/",
    ".next/",
    "__pycache__/",
    "/static_src/",          # old Tailwind build pipeline for the Django theme
    "backend/app/theme/",
    "frontend/public/",
    "/dist/",
    "/build/",
    ".git/",
)

# File suffixes we never index as source.
EXCLUDE_SUFFIXES = (".bak", ".lock", ".map", ".min.js", ".min.css", "-lock.json")

SRC_EXTS = (".py", ".ts", ".tsx", ".js", ".jsx")

# Area labels by path prefix (first match wins).
AREA_RULES = (
    ("backend/app/catalog/management/commands/", "backend:mgmt"),
    ("backend/app/catalog/migrations/", "backend:migrations"),
    ("backend/app/catalog/tests/", "backend:tests"),
    ("backend/app/catalog/", "backend:catalog"),
    ("backend/app/etl/", "backend:etl"),
    ("backend/app/config/", "backend:config"),
    ("backend/app/scripts/", "backend:scripts"),
    ("backend/", "backend:other"),
    ("frontend/app/", "frontend:routes"),
    ("frontend/components/", "frontend:components"),
    ("frontend/lib/", "frontend:lib"),
    ("frontend/", "frontend:other"),
    ("scripts/", "scripts"),
)


def area_for(path: str) -> str:
    for prefix, label in AREA_RULES:
        if path.startswith(prefix):
            return label
    return "other"


# --------------------------------------------------------------------------- #
# Git helpers
# --------------------------------------------------------------------------- #

def run_git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True, encoding="utf-8", errors="replace").strip()


def repo_root() -> str:
    return run_git("rev-parse", "--show-toplevel")


def tracked_files(root: str) -> list[str]:
    out = run_git("-C", root, "ls-files")
    files = []
    for line in out.splitlines():
        p = line.strip().replace("\\", "/")
        if not p:
            continue
        if any(part in ("/" + p if not p.startswith(part) else p) for part in EXCLUDE_DIR_PARTS):
            # robust contains-check below
            pass
        if _excluded(p):
            continue
        files.append(p)
    return files


def _excluded(p: str) -> bool:
    probe = "/" + p
    if any(part in probe for part in EXCLUDE_DIR_PARTS):
        return True
    if p.endswith(EXCLUDE_SUFFIXES):
        return True
    return False


# --------------------------------------------------------------------------- #
# Symbol model
# --------------------------------------------------------------------------- #

class Sym:
    __slots__ = ("name", "kind", "area", "path", "line", "extra")

    def __init__(self, name, kind, area, path, line, extra=""):
        self.name = name
        self.kind = kind
        self.area = area
        self.path = path
        self.line = line
        self.extra = extra

    @property
    def loc(self) -> str:
        return f"{self.path}:{self.line}"


# --------------------------------------------------------------------------- #
# Python extraction
# --------------------------------------------------------------------------- #

RE_PY_CLASS = re.compile(r"^class\s+(\w+)\s*(?:\(([^)]*)\))?\s*:")
RE_PY_DEF = re.compile(r"^(?:async\s+)?def\s+(\w+)\s*\(")
RE_URL_PATH = re.compile(r"""\bpath\(\s*r?['"]([^'"]*)['"]\s*,\s*([\w.]+)""")
RE_URL_REGISTER = re.compile(r"""\brouter\.register\(\s*r?['"]([^'"]*)['"]\s*,\s*(\w+)""")


def classify_py_class(name: str, bases: str) -> str:
    b = bases or ""
    if "models.Model" in b:
        return "model"
    if "models.Manager" in b or "Manager" in b and "models" in b:
        return "manager"
    if "Serializer" in b:
        return "serializer"
    if any(t in b for t in ("ViewSet", "APIView", "generics.", "ListAPIView", "GenericAPIView")):
        return "view"
    if any(t in b for t in ("TextChoices", "IntegerChoices", "Enum")):
        return "enum"
    if "Middleware" in b or name.endswith("Middleware"):
        return "middleware"
    if "Command" in b:
        return "mgmt-command"
    if "Config" in b or "AppConfig" in b:
        return "appconfig"
    return "class"


def extract_python(path: str, area: str, lines: list[str]) -> list[Sym]:
    syms: list[Sym] = []
    is_url = path.endswith("urls.py")
    is_mgmt = "/management/commands/" in path

    if is_mgmt:
        cmd = os.path.basename(path)[:-3]
        if not cmd.startswith("_"):
            syms.append(Sym(cmd, "mgmt-command", area, path, 1, "manage.py " + cmd))

    for i, raw in enumerate(lines, 1):
        m = RE_PY_CLASS.match(raw)
        if m:
            name, bases = m.group(1), m.group(2) or ""
            syms.append(Sym(name, classify_py_class(name, bases), area, path, i, bases.strip()))
            continue
        # top-level functions only (column 0) — these are FBV endpoints / tasks / helpers
        if raw and not raw[0].isspace():
            m = RE_PY_DEF.match(raw)
            if m:
                name = m.group(1)
                kind = "task" if path.endswith("_tasks.py") else "function"
                syms.append(Sym(name, kind, area, path, i))
                continue
        if is_url:
            mu = RE_URL_PATH.search(raw)
            if mu:
                route, view = mu.group(1), mu.group(2)
                syms.append(Sym("/api/" + route, "endpoint", area, path, i, "→ " + view))
            mr = RE_URL_REGISTER.search(raw)
            if mr:
                base, vs = mr.group(1), mr.group(2)
                syms.append(Sym("/api/" + base + "/", "endpoint", area, path, i, "→ " + vs + " (router)"))
    return syms


# --------------------------------------------------------------------------- #
# TS / TSX extraction
# --------------------------------------------------------------------------- #

RE_EXPORT = re.compile(
    r"^export\s+(?:default\s+)?(?:async\s+)?"
    r"(function|class|const|let|interface|type|enum)\s+(\w+)"
)
RE_EXPORT_DEFAULT_FN = re.compile(r"^export\s+default\s+function\s+(\w+)")


def next_route_for(path: str) -> str | None:
    # frontend/app/<segments>/page.tsx  ->  /<segments>
    if not path.startswith("frontend/app/"):
        return None
    base = os.path.basename(path)
    if base not in ("page.tsx", "page.ts", "route.ts", "route.tsx", "layout.tsx"):
        return None
    seg = path[len("frontend/app/"):]
    seg = seg.rsplit("/", 1)[0] if "/" in seg else ""
    # strip Next.js route-group folders "(group)"
    parts = [p for p in seg.split("/") if p and not (p.startswith("(") and p.endswith(")"))]
    route = "/" + "/".join(parts)
    if base.startswith("route"):
        return route + "  (API route)"
    if base == "layout.tsx":
        return route + "  (layout)"
    return route or "/"


def classify_ts(kind_kw: str, name: str, is_tsx: bool) -> str:
    if kind_kw == "interface":
        return "interface"
    if kind_kw == "type":
        return "type"
    if kind_kw == "enum":
        return "enum"
    if name.startswith("use") and len(name) > 3 and name[3].isupper():
        return "hook"
    if name[:1].isupper():
        return "component" if is_tsx else "class" if kind_kw == "class" else "const"
    if kind_kw == "function":
        return "function"
    return "const"


def extract_ts(path: str, area: str, lines: list[str]) -> list[Sym]:
    syms: list[Sym] = []
    is_tsx = path.endswith(".tsx") or path.endswith(".jsx")

    route = next_route_for(path)
    if route is not None:
        syms.append(Sym(route, "route", area, path, 1))

    for i, raw in enumerate(lines, 1):
        md = RE_EXPORT_DEFAULT_FN.match(raw)
        if md:
            name = md.group(1)
            syms.append(Sym(name, "component" if is_tsx else "function", area, path, i, "default export"))
            continue
        m = RE_EXPORT.match(raw)
        if m:
            kw, name = m.group(1), m.group(2)
            syms.append(Sym(name, classify_ts(kw, name, is_tsx), area, path, i))
    return syms


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #

def read_lines(abspath: str) -> list[str]:
    try:
        with open(abspath, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()
    except (OSError, UnicodeError):
        return []


def build(root: str):
    files = tracked_files(root)
    src_files = [f for f in files if f.endswith(SRC_EXTS)]

    all_syms: list[Sym] = []
    for rel in src_files:
        area = area_for(rel)
        if area == "backend:migrations":
            continue  # skip migration churn — indexed only as a count
        lines = read_lines(os.path.join(root, rel))
        if rel.endswith(".py"):
            all_syms.extend(extract_python(rel, area, lines))
        else:
            all_syms.extend(extract_ts(rel, area, lines))

    return files, src_files, all_syms


# --------------------------------------------------------------------------- #
# Emit
# --------------------------------------------------------------------------- #

def md_link(loc: str) -> str:
    return loc  # plain `path:line` is grep-friendly and clickable in most editors


def write_symbols_tsv(out_dir: str, syms: list[Sym]):
    rows = sorted(syms, key=lambda s: (s.area, s.path, s.line))
    with open(os.path.join(out_dir, "symbols.tsv"), "w", encoding="utf-8") as fh:
        fh.write("# name\tkind\tarea\tlocation\textra\n")
        fh.write("# grep this file first. e.g.  grep -iP '\\tmodel\\t' symbols.tsv\n")
        for s in rows:
            fh.write(f"{s.name}\t{s.kind}\t{s.area}\t{s.loc}\t{s.extra}\n")


def group(syms, pred):
    return sorted([s for s in syms if pred(s)], key=lambda s: (s.path, s.line))


def section(fh, title, items, show_extra=True):
    fh.write(f"## {title}  ({len(items)})\n\n")
    if not items:
        fh.write("_none_\n\n")
        return
    cur = None
    for s in items:
        if s.path != cur:
            cur = s.path
            fh.write(f"\n**{s.path}**\n\n")
        extra = f" — {s.extra}" if (show_extra and s.extra) else ""
        fh.write(f"- `{s.name}` ({s.kind}) → {s.loc}{extra}\n")
    fh.write("\n")


def write_backend_md(out_dir: str, syms: list[Sym]):
    be = [s for s in syms if s.area.startswith("backend")]
    with open(os.path.join(out_dir, "backend.md"), "w", encoding="utf-8") as fh:
        fh.write("# Backend index (Django / DRF / ETL)\n\n")
        fh.write("Generated by `build_index.py`. Locations are `path:line` from repo root.\n\n")
        section(fh, "API endpoints (urls.py)", group(be, lambda s: s.kind == "endpoint"))
        section(fh, "Models", group(be, lambda s: s.kind == "model"))
        section(fh, "DRF views / viewsets", group(be, lambda s: s.kind == "view"))
        section(fh, "Serializers", group(be, lambda s: s.kind == "serializer"))
        section(fh, "Function views & helpers (catalog)",
                group(be, lambda s: s.kind == "function" and s.area == "backend:catalog"))
        section(fh, "Background tasks", group(be, lambda s: s.kind == "task"))
        section(fh, "ETL (sources / destinations / hooks)",
                group(be, lambda s: s.area == "backend:etl"))
        section(fh, "Management commands", group(be, lambda s: s.kind == "mgmt-command"))
        section(fh, "Other classes (managers, enums, middleware, config)",
                group(be, lambda s: s.kind in ("manager", "enum", "middleware", "appconfig", "class")
                       and s.area not in ("backend:tests",)))


def write_frontend_md(out_dir: str, syms: list[Sym]):
    fe = [s for s in syms if s.area.startswith("frontend")]
    with open(os.path.join(out_dir, "frontend.md"), "w", encoding="utf-8") as fh:
        fh.write("# Frontend index (Next.js / React / TS)\n\n")
        fh.write("Generated by `build_index.py`. Locations are `path:line` from repo root.\n\n")
        section(fh, "Routes / pages (App Router)", group(fe, lambda s: s.kind == "route"))
        section(fh, "Components", group(fe, lambda s: s.kind == "component"), show_extra=False)
        section(fh, "Hooks", group(fe, lambda s: s.kind == "hook"))
        section(fh, "Lib functions & helpers",
                group(fe, lambda s: s.area == "frontend:lib" and s.kind in ("function", "const")))
        section(fh, "Types & interfaces",
                group(fe, lambda s: s.kind in ("interface", "type", "enum")))


def file_tree(files: list[str], max_depth: int = 3) -> str:
    seen = set()
    out = []
    for f in sorted(files):
        parts = f.split("/")
        for d in range(1, min(len(parts), max_depth)):
            sub = "/".join(parts[:d])
            if sub not in seen:
                seen.add(sub)
                out.append("  " * (d - 1) + parts[d - 1] + "/")
    return "\n".join(out)


def write_index_md(out_dir: str, root: str, files, src_files, syms, manifest):
    by_kind = defaultdict(int)
    by_area = defaultdict(int)
    for s in syms:
        by_kind[s.kind] += 1
        by_area[s.area] += 1

    with open(os.path.join(out_dir, "INDEX.md"), "w", encoding="utf-8") as fh:
        fh.write("# Project index\n\n")
        fh.write(f"> Generated by `.claude/skills/index-project/build_index.py` "
                 f"from git commit `{manifest['commit']}`.\n"
                 f"> Re-run `/index-project` (or `python .claude/skills/index-project/build_index.py`) "
                 f"after significant changes.\n\n")
        fh.write("## How to search fast\n\n")
        fh.write(
            "1. **Find where a symbol lives** — grep the flat table first:\n"
            "   - `grep -i \"PaymentForm\" .claude/index/symbols.tsv`\n"
            "   - by kind: `grep -P \"\\tmodel\\t\" .claude/index/symbols.tsv`\n"
            "   - by area: `grep -P \"\\tbackend:etl\\t\" .claude/index/symbols.tsv`\n"
            "2. **Browse a layer** — open `backend.md` or `frontend.md` (grouped by role).\n"
            "3. **Only then** open the source file at the `path:line` you found.\n\n"
        )
        fh.write("## Stats\n\n")
        fh.write(f"- Tracked files indexed: **{len(files)}** ({len(src_files)} source files)\n")
        fh.write(f"- Symbols extracted: **{len(syms)}**\n\n")
        fh.write("| kind | count |\n|---|---|\n")
        for k, n in sorted(by_kind.items(), key=lambda kv: -kv[1]):
            fh.write(f"| {k} | {n} |\n")
        fh.write("\n| area | symbols |\n|---|---|\n")
        for a, n in sorted(by_area.items(), key=lambda kv: -kv[1]):
            fh.write(f"| {a} | {n} |\n")
        fh.write("\n## Index files\n\n")
        fh.write("- `symbols.tsv` — flat, grep-optimized: `name<TAB>kind<TAB>area<TAB>location<TAB>extra`\n")
        fh.write("- `backend.md` — endpoints, models, views, serializers, tasks, ETL, commands\n")
        fh.write("- `frontend.md` — routes, components, hooks, lib helpers, types\n")
        fh.write("- `manifest.json` — provenance (commit, counts) for staleness checks\n\n")
        fh.write("## Authored docs\n\n")
        fh.write("Hand-written architecture docs live in `docs/` "
                 "(architecture, api, database, etl, lineage, assistant, governance, frontend, "
                 "local-development). Read those for *why*; use this index for *where*.\n\n")
        fh.write("## Directory map (top levels)\n\n```\n")
        fh.write(file_tree([f for f in files], max_depth=3))
        fh.write("\n```\n")


def write_manifest(out_dir: str, manifest: dict):
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)


# --------------------------------------------------------------------------- #
# Staleness
# --------------------------------------------------------------------------- #

def current_commit(root: str) -> str:
    try:
        return run_git("-C", root, "rev-parse", "--short", "HEAD")
    except subprocess.CalledProcessError:
        return "unknown"


def check_stale(root: str, out_dir: str) -> bool:
    mpath = os.path.join(out_dir, "manifest.json")
    if not os.path.exists(mpath):
        print("STALE: no index found — run the indexer.")
        return True
    with open(mpath, encoding="utf-8") as fh:
        manifest = json.load(fh)
    cur = current_commit(root)
    if manifest.get("commit") != cur:
        print(f"STALE: index built at {manifest.get('commit')}, HEAD is {cur}.")
        return True
    dirty = run_git("-C", root, "status", "--porcelain")
    if dirty:
        print("STALE: working tree has uncommitted changes since the index was built.")
        return True
    print(f"FRESH: index matches HEAD {cur}.")
    return False


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main(argv: list[str]) -> int:
    try:  # avoid UnicodeEncodeError on non-UTF-8 Windows consoles
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    root = repo_root()
    out_dir = os.path.join(root, ".claude", "index")

    if "--check" in argv:
        return 1 if check_stale(root, out_dir) else 0

    os.makedirs(out_dir, exist_ok=True)
    files, src_files, syms = build(root)

    manifest = {
        "commit": current_commit(root),
        "file_count": len(files),
        "source_file_count": len(src_files),
        "symbol_count": len(syms),
    }

    write_symbols_tsv(out_dir, syms)
    write_backend_md(out_dir, syms)
    write_frontend_md(out_dir, syms)
    write_index_md(out_dir, root, files, src_files, syms, manifest)
    write_manifest(out_dir, manifest)

    print(f"Indexed {len(src_files)} source files -> {len(syms)} symbols.")
    print(f"Wrote: {out_dir}")
    for name in ("INDEX.md", "symbols.tsv", "backend.md", "frontend.md", "manifest.json"):
        print(f"  - .claude/index/{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
