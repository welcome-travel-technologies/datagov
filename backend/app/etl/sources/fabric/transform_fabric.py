import os
import json
import re
import hashlib
import sys
import collections
import functools

import pandas as pd

# Pre-compiled regex patterns (avoids recompilation in hot loops)
_RE_CLEAN_ID = re.compile(r"[\s'\"`]+")
_RE_PROPERTY = re.compile(r'"Property"\s*:\s*"([^"]+)"')
_RE_NAME_FIELD = re.compile(r'"Name"\s*:\s*"([^"]+)"')
_RE_VISUAL_NAME = re.compile(r'"name"\s*:\s*"([^"]+)"')
_RE_DAX_BRACKET = re.compile(r'\[([^\]]+)\]')
_RE_DAX_TABLE_COL = re.compile(r"'([^']+)'\[([^\]]+)\]")
_RE_DAX_WORD = re.compile(r'\b[a-zA-Z0-9_]+\b')
_RE_SQL_FROM = re.compile(r'FROM\s+`?([\w-]+)`?\.`?([\w-]+)`?\.`?([\w-]+)`?', re.IGNORECASE)
_RE_BQ_PROJECT = re.compile(r'\[Name\s*=\s*"?([^",\]]+)"?')
_RE_BQ_SCHEMA = re.compile(r'\{\[Name\s*=\s*"?([^",]+)"?\s*,\s*Kind\s*=\s*"?Schema"?\s*\]\}')
_RE_BQ_OBJECT = re.compile(r'\{\[Name\s*=\s*"?([^",]+)"?\s*,\s*Kind\s*=\s*"?(View|Table)"?\s*\]\}')

@functools.lru_cache(maxsize=None)
def generate_custom_id(*args):
    """Generates a custom MD5 hash ID by lowercasing and removing spaces and quotes."""
    combined = "_".join(str(arg) for arg in args if arg is not None and pd.notna(arg))
    cleaned = _RE_CLEAN_ID.sub("", combined.lower())
    return hashlib.md5(cleaned.encode('utf-8')).hexdigest()

def get_item_id(r, category):
    """Calculates final item_id prioritizing lineage_tag, falling back to name/hierarchy."""
    dataset_id = r.get('dataset_id')
    lineage_tag = r.get('lineage_tag')
    
    if pd.notna(lineage_tag) and lineage_tag:
        return generate_custom_id(dataset_id, lineage_tag)
        
    if category == "Tables":
        return generate_custom_id(dataset_id, r.get('name'))
    else:
        # For Columns and Measures
        return generate_custom_id(dataset_id, r.get('table_name'), r.get('name'))

# ==========================================
# PARSERS (Transform)
# ==========================================

def extract_fields_from_visual(visual_config_str):
    """Uses regex to extract Column and Measure names from a visual's config JSON string."""
    fields = set()
    
    # We want to ignore internal aliases like "m", "0", "01", etc.
    # Actual fields/measures usually have symbols, dots, or are reasonably long words.
    
    # Pattern 1: {"Property": "City"}
    for match in _RE_PROPERTY.findall(visual_config_str):
        # Filter out short junk (1-2 chars) or purely numeric matches
        if len(match) > 2 and not match.isdigit():
            fields.add(match)
        
    # Pattern 2: {"Name": "Table.ColumnName"}
    for match in _RE_NAME_FIELD.findall(visual_config_str):
        # "Name" attributes in visual JSON are highly reliable for lineages
        if len(match) > 2 and not match.isdigit():
            fields.add(match)
            
    return fields

def parse_report_layout_pbir(report_dir, workspace_id, workspace_name, report_name):
    """PBIR-format equivalent of parse_report_layout.

    Newer Fabric reports ship as a folder tree instead of a single report.json:
      <report>/definition/report.json            (no `sections` here anymore)
      <report>/definition/pages/<id>/page.json   (one file per page, has displayName)
      <report>/definition/pages/<id>/visuals/<id>/visual.json  (one file per visual)

    Returns the same (usage, stats) shape as parse_report_layout so callers can
    treat both formats uniformly.
    """
    usage = []
    stats = {
        "workspace_id": workspace_id,
        "workspace_name": workspace_name,
        "report_name": report_name,
        "total_pages": 0,
        "total_visuals": 0
    }

    pages_dir = os.path.join(report_dir, "definition", "pages")
    if not os.path.isdir(pages_dir):
        return usage, stats

    try:
        for page_entry in os.listdir(pages_dir):
            page_path = os.path.join(pages_dir, page_entry)
            page_json = os.path.join(page_path, "page.json")
            if not os.path.isfile(page_json):
                continue

            try:
                with open(page_json, 'r', encoding='utf-8') as f:
                    page_data = json.load(f)
            except Exception as e:
                print(f"[WARNING] Failed to parse {page_json}: {e}")
                continue

            page_name = page_data.get('displayName') or page_entry
            stats["total_pages"] += 1

            visuals_dir = os.path.join(page_path, "visuals")
            if not os.path.isdir(visuals_dir):
                continue

            for visual_entry in os.listdir(visuals_dir):
                visual_json_path = os.path.join(visuals_dir, visual_entry, "visual.json")
                if not os.path.isfile(visual_json_path):
                    continue
                stats["total_visuals"] += 1

                try:
                    with open(visual_json_path, 'r', encoding='utf-8') as f:
                        config_str = f.read()
                except Exception as e:
                    print(f"[WARNING] Failed to read {visual_json_path}: {e}")
                    continue

                # Visual id: the JSON root has a "name": "<id>" key; fall back to
                # the folder name (which is the same id in practice).
                v_name_match = _RE_VISUAL_NAME.search(config_str)
                visual_id = v_name_match.group(1) if v_name_match else visual_entry

                # Title path in PBIR: visual.visualContainerObjects.title[0]
                #   .properties.text.expr.Literal.Value
                # (Legacy was singleVisual.vcObjects.title — different parent key.)
                visual_title = "Unknown"
                try:
                    c_dict = json.loads(config_str)
                    title_objs = (
                        c_dict.get("visual", {})
                              .get("visualContainerObjects", {})
                              .get("title", [])
                    )
                    if title_objs:
                        text_expr = title_objs[0].get("properties", {}).get("text", {}).get("expr", {})
                        if "Literal" in text_expr:
                            visual_title = str(text_expr["Literal"].get("Value", "Unknown")).strip("'")
                except Exception:
                    pass

                visual_name = f"{visual_title} ({visual_id})"
                visual_id = visual_name  # Match legacy parser's id-formatting behaviour

                # Field extraction: same regexes, applied to the whole visual.json
                # text. Property/Name/queryRef shapes are preserved in PBIR.
                page_fields = set()
                page_fields.update(extract_fields_from_visual(config_str))

                if not page_fields:
                    usage.append({
                        "workspace_id": workspace_id,
                        "workspace_name": workspace_name,
                        "report_name": report_name,
                        "page_name": page_name,
                        "visual_id": visual_id,
                        "field_name": None
                    })
                else:
                    for field in page_fields:
                        usage.append({
                            "workspace_id": workspace_id,
                            "workspace_name": workspace_name,
                            "report_name": report_name,
                            "page_name": page_name,
                            "visual_id": visual_id,
                            "field_name": field
                        })
    except Exception as e:
        print(f"[WARNING] Failed to parse PBIR report at {report_dir}: {e}")

    return usage, stats


def parse_report_layout(report_json_path, workspace_id, workspace_name, report_name):
    """Extracts used fields per page from report.json and computes stats."""
    usage = []
    stats = {
        "workspace_id": workspace_id,
        "workspace_name": workspace_name,
        "report_name": report_name,
        "total_pages": 0,
        "total_visuals": 0
    }
    
    try:
        with open(report_json_path, 'r', encoding='utf-8') as f:
            report_layout_json = json.load(f)
            
        sections = report_layout_json.get('sections', [])
        stats["total_pages"] = len(sections)
        
        for section in sections:
            page_name = section.get('displayName')
            visuals = section.get('visualContainers', [])
            stats["total_visuals"] += len(visuals)
            
            for i, visual in enumerate(visuals):
                config_str = visual.get('config', '')
                if isinstance(config_str, dict): config_str = json.dumps(config_str)
                
                v_name_match = _RE_VISUAL_NAME.search(config_str)
                visual_id = v_name_match.group(1) if v_name_match else f"{page_name}_visual_{i}"
                
                # EXTRACT TITLE
                visual_title = "Unknown"
                try:
                    c_dict = json.loads(config_str)
                    title_objs = c_dict.get("singleVisual", {}).get("vcObjects", {}).get("title", [])
                    if title_objs:
                        text_expr = title_objs[0].get("properties", {}).get("text", {}).get("expr", {})
                        if "Literal" in text_expr:
                            visual_title = str(text_expr["Literal"].get("Value", "Unknown")).strip("'")
                except:
                    pass
                
                visual_name = f"{visual_title} ({visual_id})"
                visual_id = visual_name # Override visual_id to use the new formatted name
                
                page_fields = set()
                page_fields.update(extract_fields_from_visual(config_str))
                
                filters_str = visual.get('filters', '')
                if isinstance(filters_str, (dict, list)): filters_str = json.dumps(filters_str)
                page_fields.update(extract_fields_from_visual(filters_str))
            
                if not page_fields:
                    usage.append({
                        "workspace_id": workspace_id,
                        "workspace_name": workspace_name,
                        "report_name": report_name,
                        "page_name": page_name,
                        "visual_id": visual_id,
                        "field_name": None
                    })
                else:
                    for field in page_fields:
                        usage.append({
                            "workspace_id": workspace_id,
                            "workspace_name": workspace_name,
                            "report_name": report_name,
                            "page_name": page_name,
                            "visual_id": visual_id,
                            "field_name": field
                        })
    except Exception as e:
        print(f"[WARNING] Failed to parse {report_json_path}: {e}")
    return usage, stats

def parse_bigquery_source(m_code):
    """Parses M-Query to extract BigQuery project, schema, and table details."""
    if not m_code or not isinstance(m_code, str): return None, None, None, None

    # Only attempt BigQuery-shape parsing if the M-code actually references the
    # GoogleBigQuery connector. Otherwise the Name="..." regex below latches
    # onto SharePoint filenames, Excel sheet names, etc. and leaks them into
    # bq_project.
    if 'GoogleBigQuery' not in m_code and 'Value.NativeQuery' not in m_code:
        return None, None, None, None

    # Native SQL: FROM `project.dataset.table` or FROM project.dataset.table
    sql_pattern = _RE_SQL_FROM.search(m_code)
    if sql_pattern: return sql_pattern.group(1), sql_pattern.group(2), sql_pattern.group(3), "SQL Query"

    # Standard Navigation (Handling potential double-quote escaping in TMDL: "")
    project_match = _RE_BQ_PROJECT.search(m_code)
    schema_match = _RE_BQ_SCHEMA.search(m_code)
    object_match = _RE_BQ_OBJECT.search(m_code)

    project = project_match.group(1) if project_match else None
    schema = schema_match.group(1) if schema_match else None
    obj_name = object_match.group(1) if object_match else None
    obj_kind = object_match.group(2) if object_match else None
    return project, schema, obj_name, obj_kind

def calculate_dependencies(measures, columns, tables, table_definitions):
    """Parses DAX to generate a full dependency map across Measures, Columns, and Tables.

    Each dep row also carries `ObjectTable` (the consumer's table) and, when the
    DAX uses qualified `'Table'[Col]` syntax, `ReferencedTable` (the producer's
    table). The graph builder uses these together with the dataset_id (added
    later by the caller) to disambiguate items whose name appears in multiple
    datasets.
    """
    dependencies = []
    all_table_names = {t.lower(): t for t in tables}
    all_measure_names = {m['Name'].lower(): m['Name'] for m in measures}

    def extract_dax_deps(obj_name, obj_type, obj_table, dax):
        if not dax: return

        # [MeasureName] or [ColumnName] — bare ref, producer's table is unknown
        for match in _RE_DAX_BRACKET.findall(dax):
            match_lower = match.lower()
            if match_lower in all_measure_names:
                dependencies.append({"Object": obj_name, "ObjectType": obj_type, "ObjectTable": obj_table, "ReferencedObject": all_measure_names[match_lower], "ReferencedObjectType": "PB_MEASURE", "ReferencedTable": None})
            else:
                dependencies.append({"Object": obj_name, "ObjectType": obj_type, "ObjectTable": obj_table, "ReferencedObject": match, "ReferencedObjectType": "PB_COLUMN", "ReferencedTable": None})

        # 'TableName'[ColumnName] — producer's table is the captured `tbl_match`
        for tbl_match, col_match in _RE_DAX_TABLE_COL.findall(dax):
            dependencies.append({"Object": obj_name, "ObjectType": obj_type, "ObjectTable": obj_table, "ReferencedObject": tbl_match, "ReferencedObjectType": "PB_TABLE", "ReferencedTable": None})
            dependencies.append({"Object": obj_name, "ObjectType": obj_type, "ObjectTable": obj_table, "ReferencedObject": col_match, "ReferencedObjectType": "PB_COLUMN", "ReferencedTable": tbl_match})

        # Bare TableName (e.g. FILTER(Sales, ...))
        for word in _RE_DAX_WORD.findall(dax):
            if word.lower() in all_table_names and word.lower() != obj_name.lower(): # don't reference self in simple loops
                dependencies.append({"Object": obj_name, "ObjectType": obj_type, "ObjectTable": obj_table, "ReferencedObject": all_table_names[word.lower()], "ReferencedObjectType": "PB_TABLE", "ReferencedTable": None})

    for m in measures:
        extract_dax_deps(m['Name'], "PB_MEASURE", m.get('Table_Name'), m.get('Expression', ''))
    for c in columns:
        if c.get('Type') == 'calculated':
            extract_dax_deps(c['Name'], "PB_COLUMN", c.get('Table_Name'), c.get('Expression', ''))

    # Extract dependencies for Calculated Tables (if they have partitions with DAX 'calculated' mode)
    for td in table_definitions:
        for part in td.get('partitions', []):
            if part.get('mode') == 'calculated' or part.get('source', {}).get('type') == 'calculated':
                dax = part.get('source', {}).get('expression', '')
                if isinstance(dax, list): dax = "\n".join(dax)
                if dax:
                    extract_dax_deps(td['name'], "PB_TABLE", td['name'], dax)

    # Deduplicate using fixed-key tuples (faster than hashing full dict items)
    seen = set()
    unique_deps = []
    for d in dependencies:
        key = (d["Object"], d["ObjectType"], d["ObjectTable"], d["ReferencedObject"], d["ReferencedObjectType"], d["ReferencedTable"])
        if key not in seen:
            seen.add(key)
            unique_deps.append(d)
    return unique_deps

# Regex for opening a column / measure / partition TMDL block.
# Captures: (1) object name, (2) optional inline DAX after `=`.
TMDL_BLOCK_HEADER_RE = re.compile(
    r"^\s*(?P<kind>column|measure|partition)\s+[']?(?P<name>[^=']+?)[']?\s*(?:=\s*(?P<dax>.*))?$"
)

def _partition_type_from_dax(dax_str):
    """Classifies a partition body as 'calculated' (DAX) or 'm' (M query)."""
    stripped = dax_str.strip()
    if not stripped:
        return "m"
    # Explicit calculated markers
    if (
        "mode: calculated" in dax_str
        or stripped.startswith("=")
        or stripped.startswith("EVALUATE")
        or stripped.startswith("DATATABLE")
        or stripped.startswith("CALENDAR")
    ):
        return "calculated"
    # M-queries are wrapped in `let ... in ...`. Detect `let` as a leading token.
    if re.search(r"(^|\s)let(\s|$)", dax_str):
        return "m"
    # Fallback: assume calculated
    return "calculated"

def parse_tmdl_table(tmdl_path):
    """Extracts objects and DAX from Tabular Model Definition Language (TMDL)."""
    with open(tmdl_path, 'r', encoding='utf-8') as f:
        tmdl_content = f.read()

    lines = tmdl_content.splitlines()
    tbl_name = "Unknown"
    tbl_lineage_tag = None
    columns, measures, partitions = [], [], []
    current_block, current_obj, current_dax = None, None, []
    current_desc = ""
    current_lineage_tag = None
    current_data_type = None
    current_format_string = None
    # Whether the currently-open column/measure block was opened with an inline `= DAX` expression.
    # This is the canonical TMDL marker for a calculated column; data columns use `sourceColumn:` instead.
    current_is_calculated = False

    # Sometimes descriptions are written above the measure as /// comments
    pending_desc = []

    def save_current():
        nonlocal current_desc, current_lineage_tag, current_is_calculated
        nonlocal current_data_type, current_format_string
        if current_obj and current_block:
            dax_str = "\n".join(current_dax).strip()
            if current_block == 'column':
                columns.append({
                    "name": current_obj,
                    "expression": dax_str,
                    "type": "calculated" if current_is_calculated else "data",
                    "description": current_desc,
                    "lineage_tag": current_lineage_tag,
                    "dataType": current_data_type,
                    "formatString": current_format_string,
                })
            elif current_block == 'measure':
                measures.append({
                    "name": current_obj,
                    "expression": dax_str,
                    "description": current_desc,
                    "lineage_tag": current_lineage_tag,
                    "formatString": current_format_string,
                })
            elif current_block == 'partition':
                part_type = _partition_type_from_dax(dax_str)
                partitions.append({
                    "mode": part_type,
                    "source": {"expression": dax_str, "type": part_type},
                })
        current_desc = ""
        current_lineage_tag = None
        current_data_type = None
        current_format_string = None
        current_is_calculated = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Capture /// comments which act as descriptions in TMDL
        if stripped.startswith("///"):
            pending_desc.append(stripped.replace("///", "").strip())
            continue
        if stripped.startswith("//"):
            continue

        if line.startswith("table ") or line.startswith("table '"):
            tbl_match = re.match(r"table\s+[']?([^']+)[']?", line)
            if tbl_match:
                tbl_name = tbl_match.group(1)
            continue

        # New block opener: `column X`, `measure X`, or `partition X`, optionally
        # followed by an inline `= <DAX>` which marks a calculated column/measure.
        header = TMDL_BLOCK_HEADER_RE.match(line)
        if header:
            save_current()
            kind = header.group("kind")
            inline_dax = header.group("dax")
            current_block = kind
            current_obj = header.group("name").strip()
            current_dax = [inline_dax] if inline_dax else []
            current_is_calculated = bool(inline_dax) if kind in ("column", "measure") else False
            if kind in ("column", "measure") and pending_desc:
                current_desc = " ".join(pending_desc)
            pending_desc = []
            continue
            
        # Catch lineageTag mapped as properties
        if line.strip().startswith("lineageTag:"):
            lt_match = re.match(r'^\s*lineageTag:\s*(.*)', line)
            if lt_match:
                lt = lt_match.group(1).strip()
                if current_block is None:
                    tbl_lineage_tag = lt
                else:
                    current_lineage_tag = lt
                    if current_block == 'measure' and current_obj:
                        for m in measures:
                            if m['name'] == current_obj:
                                m['lineage_tag'] = current_lineage_tag
                    elif current_block == 'column' and current_obj:
                        for c in columns:
                            if c['name'] == current_obj:
                                c['lineage_tag'] = current_lineage_tag
            continue

        # Catch descriptions mapped as properties (e.g. description: "...")
        if line.strip().startswith("description:"):
            desc_match = re.match(r'^\s*description:\s*(.*)', line)
            if desc_match:
                current_desc = desc_match.group(1).strip('"\'')
                # If we are already in a measure block, attach it retroactively
                if current_block == 'measure' and current_obj:
                    # Update the last added measure if it matches
                    for m in measures:
                        if m['name'] == current_obj:
                            m['description'] = current_desc
                elif current_block == 'column' and current_obj:
                    for c in columns:
                        if c['name'] == current_obj:
                            c['description'] = current_desc
            continue

        # Capture dataType (column blocks only — TMDL spec) and formatString
        # (column or measure blocks). Without these, the generic `: in stripped`
        # skip below drops the property silently.
        if current_block in ('column', 'measure') and stripped.startswith("dataType:"):
            dt_match = re.match(r'^\s*dataType:\s*(.*)', line)
            if dt_match:
                current_data_type = dt_match.group(1).strip().strip('"\'') or None
            continue
        if current_block in ('column', 'measure') and stripped.startswith("formatString:"):
            fs_match = re.match(r'^\s*formatString:\s*(.*)', line)
            if fs_match:
                current_format_string = fs_match.group(1).strip().strip('"\'') or None
            continue

        if current_block:
            # Avoid picking up nested property definitions like `formatString: 0` unless it's a DAX block
            # For measures and columns, DAX formulas don't typically contain `dataType:` or `formatString:`
            # But M queries definitely do have deep indentation.
            if ":" in stripped and not stripped.startswith("let") and not current_block == 'partition':
                # It's a property (like summarizeBy: none), ignore it.
                continue
            if stripped == "source =":
                continue # Ignore the literal 'source =' line in partitions
                
            current_dax.append(line.strip())
            
    save_current()
    return {"name": tbl_name, "lineage_tag": tbl_lineage_tag, "columns": columns, "measures": measures, "partitions": partitions}

# Header for a relationship block in relationships.tmdl. The block is a
# free-floating GUID after the keyword `relationship`, e.g.
#     relationship 43810166-de61-4850-9dd5-78141ad701f6
TMDL_RELATIONSHIP_HEADER_RE = re.compile(r"^\s*relationship\s+(?P<guid>\S+)\s*$")
# A column reference inside a relationship: `Views.Date` or `'My Table'.Date`.
TMDL_REL_COL_RE = re.compile(r"^\s*(?:'([^']+)'|([^.\s]+))\.(.+?)\s*$")


def parse_tmdl_relationships(tmdl_path):
    """
    Parses `definition/relationships.tmdl` into a list of relationship dicts.

    Each block looks like:
        relationship <guid>
            crossFilteringBehavior: bothDirections
            fromCardinality: one
            isActive: false
            fromColumn: Views.Date
            toColumn: Dates.Date

    Returns: [
        {
            "guid": "...",
            "from_table": "Views", "from_column": "Date",
            "to_table": "Dates",   "to_column": "Date",
            "from_cardinality": "many"|"one" (default "many"),
            "to_cardinality":   "many"|"one" (default "one"),
            "cross_filtering": "single"|"bothDirections"|"none" (default "single"),
            "is_active": True|False (default True),
        },
        ...
    ]
    """
    if not os.path.exists(tmdl_path):
        return []

    with open(tmdl_path, 'r', encoding='utf-8') as f:
        content = f.read()

    rels = []
    current = None

    def _split_col(ref):
        m = TMDL_REL_COL_RE.match(ref)
        if not m:
            return None, None
        table = m.group(1) or m.group(2)
        col = m.group(3).strip().strip("'")
        return table, col

    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if line.strip().startswith("//") or line.strip().startswith("///"):
            continue

        header = TMDL_RELATIONSHIP_HEADER_RE.match(line)
        if header:
            if current and current.get('from_table') and current.get('to_table'):
                rels.append(current)
            # TMDL omits cardinality / cross-filter when the value is the
            # default. Defaults from the Tabular Object Model spec:
            #   fromCardinality = many, toCardinality = one,
            #   crossFilteringBehavior = single, isActive = true
            current = {
                "guid": header.group("guid"),
                "from_table": None, "from_column": None,
                "to_table": None,   "to_column": None,
                "from_cardinality": "many",
                "to_cardinality":   "one",
                "cross_filtering":  "single",
                "is_active": True,
            }
            continue

        if current is None:
            continue

        stripped = line.strip()
        if ':' not in stripped:
            continue
        key, _, value = stripped.partition(':')
        key = key.strip()
        value = value.strip()

        if key == 'fromColumn':
            t, c = _split_col(value)
            current['from_table'], current['from_column'] = t, c
        elif key == 'toColumn':
            t, c = _split_col(value)
            current['to_table'], current['to_column'] = t, c
        elif key == 'fromCardinality':
            current['from_cardinality'] = value
        elif key == 'toCardinality':
            current['to_cardinality'] = value
        elif key == 'crossFilteringBehavior':
            current['cross_filtering'] = value
        elif key == 'isActive':
            current['is_active'] = value.lower() not in ('false', '0', 'no')

    if current and current.get('from_table') and current.get('to_table'):
        rels.append(current)

    return rels


def process_semantic_model_dir(model_dir, dataset_name):
    """Parses all TMDL/BIM files in a downloaded model folder."""
    tables_list = []
    dataset_id = None
    
    meta_path = os.path.join(model_dir, "item_metadata.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                md = json.load(f)
                dataset_id = md.get('id', '')
        except: pass
    
    # 1. Parse BIM (if exists)
    bim_path = os.path.join(model_dir, "model.bim")
    if os.path.exists(bim_path):
        with open(bim_path, 'r', encoding='utf-8') as f:
            try: tables_list = json.load(f).get('model', {}).get('tables', [])
            except: pass
            
    # 2. Parse TMDL (if tables folder exists)
    tmdl_tables_dir = os.path.join(model_dir, "definition", "tables")
    if not tables_list and os.path.exists(tmdl_tables_dir):
        for file in os.listdir(tmdl_tables_dir):
            if file.endswith(".tmdl"):
                parsed_table = parse_tmdl_table(os.path.join(tmdl_tables_dir, file))
                if parsed_table["name"] != "Unknown":
                    tables_list.append(parsed_table)

    if not tables_list: return None

    tables_data, columns_data, measures_data, sources_data = [], [], [], []
    all_table_names, all_measures_lookup, all_columns_lookup = [], [], []
    
    def _coerce_expression(raw):
        """Expression can be a string or list of strings in BIM; normalise to str."""
        if isinstance(raw, str):
            return raw
        if isinstance(raw, list):
            return "\n".join(str(part) for part in raw if part is not None)
        return ""

    for tbl in tables_list:
        tbl_name = tbl.get('name')
        if not tbl_name:
            continue
        if tbl_name.startswith("DateTableTemplate") or tbl_name.startswith("LocalDateTable"):
            continue

        all_table_names.append(tbl_name)
        tables_data.append({
            "dataset_id": dataset_id,
            "dataset_name": dataset_name,
            "Name": tbl_name,
            "Description": tbl.get("description", ""),
            "lineage_tag": tbl.get("lineageTag") or tbl.get("lineage_tag"),
        })

        for col in tbl.get('columns', []):
            if col.get('isHidden'):
                continue
            col_dict = {
                "dataset_id": dataset_id,
                "dataset_name": dataset_name,
                "Table_Name": tbl_name,
                "Name": col.get("name"),
                "DataType": col.get("dataType"),
                "Type": col.get("type", "data"),
                "Expression": _coerce_expression(col.get("expression")),
                "lineage_tag": col.get("lineageTag") or col.get("lineage_tag"),
            }
            columns_data.append(col_dict)
            all_columns_lookup.append(col_dict)

        for meas in tbl.get('measures', []):
            meas_dict = {
                "dataset_id": dataset_id,
                "dataset_name": dataset_name,
                "Table_Name": tbl_name,
                "Name": meas.get("name"),
                "Expression": _coerce_expression(meas.get("expression")),
                "FormatString": meas.get("formatString", ""),
                "Description": meas.get("description", ""),
                "lineage_tag": meas.get("lineageTag") or meas.get("lineage_tag"),
            }
            measures_data.append(meas_dict)
            all_measures_lookup.append(meas_dict)
            
        for part in tbl.get('partitions', []):
            m_code = part.get('source', {}).get('expression', "")
            if isinstance(m_code, list): m_code = "\n".join(m_code)
            project, schema, obj_name, obj_kind = parse_bigquery_source(m_code)
            sources_data.append({"dataset_id": dataset_id, "dataset_name": dataset_name, "Table_Name": tbl_name, "M_Query": m_code, "BQ_Project": project, "BQ_Schema": schema, "BQ_Source_Name": obj_name, "BQ_Kind": obj_kind})

    dependencies_data = calculate_dependencies(all_measures_lookup, all_columns_lookup, all_table_names, tables_list)
    for dep in dependencies_data:
        dep["dataset_id"] = dataset_id
        dep["dataset_name"] = dataset_name

    # Relationships (definition/relationships.tmdl). Optional — many models
    # don't have any. Each row is one declared relationship in the TMDL spec.
    rel_path = os.path.join(model_dir, "definition", "relationships.tmdl")
    relationships_data = []
    seen_rel_keys = set()
    for rel in parse_tmdl_relationships(rel_path):
        # Dedup within a dataset on (from_table, from_col, to_table, to_col, is_active).
        # Two relationships with same columns but one inactive *are* legal so
        # the active flag is part of the key.
        key = (
            rel.get('from_table'), rel.get('from_column'),
            rel.get('to_table'), rel.get('to_column'),
            rel.get('is_active'),
        )
        if key in seen_rel_keys:
            continue
        seen_rel_keys.add(key)
        rel_row = {
            "dataset_id": dataset_id,
            "dataset_name": dataset_name,
            "guid": rel.get('guid'),
            "from_table": rel.get('from_table'),
            "from_column": rel.get('from_column'),
            "to_table": rel.get('to_table'),
            "to_column": rel.get('to_column'),
            "from_cardinality": rel.get('from_cardinality'),
            "to_cardinality": rel.get('to_cardinality'),
            "cross_filtering": rel.get('cross_filtering'),
            "is_active": rel.get('is_active'),
        }
        relationships_data.append(rel_row)

    return {
        "Tables": tables_data,
        "Columns": columns_data,
        "Measures": measures_data,
        "Sources": sources_data,
        "Dependencies": dependencies_data,
        "Relationships": relationships_data,
    }

# ==========================================
# MAIN EXECUTION
# ==========================================
def main():
    # Fix for Windows console UnicodeEncodeError when printing emojis in data
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass

    print("[START] Starting Transform Phase (Parsing Local Fabric Definitions)...")
    
    results = {
        "Workspaces": [], "Tables": [], "Columns": [], "Measures": [],
        "Sources": [], "Dependencies": [], "Relationships": [],
        "Reports_Lineage": [], "Reports_Stats": []
    }
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    base_dir = os.path.join(script_dir, "raw_fabric_definitions")
    
    if not os.path.exists(base_dir):
        print(f"[ERROR] Could not find '{base_dir}'. Please run the extraction script first to download the files.")
        return

    # Scan all Workspace folders
    for ws_id in os.listdir(base_dir):
        ws_path = os.path.join(base_dir, ws_id)
        if not os.path.isdir(ws_path): continue

        # 0. Extract Workspace Info
        ws_name = "Unknown"
        ws_info_file = os.path.join(ws_path, "workspace_info.json")
        if os.path.exists(ws_info_file):
            try:
                with open(ws_info_file, 'r', encoding='utf-8') as f:
                    ws_info = json.load(f)
                    ws_name = ws_info.get("name", "Unknown")
            except Exception as e:
                print(f"[WARNING] Failed to parse workspace_info.json for {ws_id}: {e}")

        # If Unknown, try to extract from a Report's definition.pbir
        if ws_name == "Unknown":
            reports_dir = os.path.join(ws_path, "Reports")
            if os.path.exists(reports_dir):
                for item_name in os.listdir(reports_dir):
                    pbir_path = os.path.join(reports_dir, item_name, "definition.pbir")
                    if os.path.exists(pbir_path):
                        try:
                            with open(pbir_path, 'r', encoding='utf-8') as f:
                                pbir_data = json.load(f)
                                conn_str = pbir_data.get("datasetReference", {}).get("byConnection", {}).get("connectionString", "")
                                match = re.search(r'myorg/([^";]+)', conn_str)
                                if match:
                                    ws_name = match.group(1).strip()
                                    break
                        except Exception:
                            pass

        results["Workspaces"].append({
            "workspace_id": ws_id,
            "workspace_name": ws_name
        })

        # 1. Parse Reports in Workspace
        reports_dir = os.path.join(ws_path, "Reports")
        if os.path.exists(reports_dir):
            for item_name in os.listdir(reports_dir):
                item_path = os.path.join(reports_dir, item_name)
                report_json = os.path.join(item_path, "report.json")
                pbir_report_json = os.path.join(item_path, "definition", "report.json")
                report_data = None
                stats = None
                if os.path.exists(report_json):
                    print(f"[REPORT] Parsing Report [{ws_id}]: {item_name}")
                    # FIX: pass both ws_id and ws_name so lineage rows have both
                    report_data, stats = parse_report_layout(report_json, ws_id, ws_name, item_name)
                elif os.path.exists(pbir_report_json):
                    # PBIR (Fabric) format: pages live under definition/pages/<id>/page.json
                    # instead of a `sections` array on the root report.json.
                    print(f"[REPORT] Parsing PBIR Report [{ws_id}]: {item_name}")
                    report_data, stats = parse_report_layout_pbir(item_path, ws_id, ws_name, item_name)

                if stats is not None:
                    # Read webUrl from item_metadata.json if available
                    metadata_path = os.path.join(item_path, "item_metadata.json")
                    if os.path.exists(metadata_path):
                        try:
                            with open(metadata_path, 'r', encoding='utf-8') as f:
                                md = json.load(f)
                                stats['web_url'] = md.get('webUrl', '')
                        except Exception as e:
                            print(f"[WARNING] Failed to read metadata for report {item_name}: {e}")

                    if report_data:
                        results["Reports_Lineage"].extend(report_data)
                    results["Reports_Stats"].append(stats)

        # 2. Parse Semantic Models in Workspace
        models_dir = os.path.join(ws_path, "SemanticModels")
        if os.path.exists(models_dir):
            for item_name in os.listdir(models_dir):
                item_path = os.path.join(models_dir, item_name)
                if os.path.exists(os.path.join(item_path, "definition", "tables")) or os.path.exists(os.path.join(item_path, "model.bim")):
                    print(f"[MODEL] Parsing Model [{ws_id}]: {item_name}")
                    model_data = process_semantic_model_dir(item_path, item_name)
                    if model_data:
                        # Add Workspace ID to datasets if needed
                        for k in ["Tables", "Columns", "Measures", "Sources", "Dependencies", "Relationships"]:
                            for row in model_data[k]:
                                row["workspace_id"] = ws_id
                            results[k].extend(model_data[k])

    # Generate Relationship Graph
    print("[GRAPH] Generating Relationship Graph...")
    graph = []

    # Pre-compute full metadata lookups (used to emit composite graph ids that
    # match `Item.item_id` for TABLE / COLUMN / MEASURE nodes so the graph and
    # the catalog share a single identity per entity).
    #
    # PRIMARY lookups use (table_name, name) as key to avoid collisions when
    # the same column/measure name appears in multiple tables (e.g. "ID").
    # SECONDARY name-only lookups are kept for resolve_field() where table
    # context isn't available (visual field references).
    columns_lookup_by_table = {}     # (table_name, col_name) -> Column dict
    columns_lookup_by_ds = {}        # (dataset_id, col_name) -> Column dict (dataset-scoped, disambiguates name twins across datasets)
    columns_lookup_by_ds_table = {}  # (dataset_id, table_name, col_name) -> Column dict (most precise)
    columns_lookup = {}              # col_name -> first matching Column dict (last-resort fallback)
    measures_lookup_by_table = {}    # (table_name, meas_name) -> Measure dict
    measures_lookup_by_ds = {}       # (dataset_id, meas_name) -> Measure dict (dataset-scoped)
    measures_lookup_by_ds_table = {} # (dataset_id, table_name, meas_name) -> Measure dict
    measures_lookup = {}             # meas_name -> first matching Measure dict (last-resort fallback)
    tables_lookup = {}               # (workspace_id, dataset_id, name) -> Table dict
    tables_lookup_by_ds = {}         # (dataset_id, name) -> Table dict (dataset-scoped)

    for c in results.get('Columns', []):
        name = c.get('Name')
        tbl = c.get('Table_Name')
        ds = c.get('dataset_id')
        if name:
            if name not in columns_lookup:
                columns_lookup[name] = c
            if tbl:
                columns_lookup_by_table.setdefault((tbl, name), c)
            if ds:
                columns_lookup_by_ds.setdefault((ds, name), c)
            if ds and tbl:
                columns_lookup_by_ds_table.setdefault((ds, tbl, name), c)
    for m in results.get('Measures', []):
        name = m.get('Name')
        tbl = m.get('Table_Name')
        ds = m.get('dataset_id')
        if name:
            if name not in measures_lookup:
                measures_lookup[name] = m
            if tbl:
                measures_lookup_by_table.setdefault((tbl, name), m)
            if ds:
                measures_lookup_by_ds.setdefault((ds, name), m)
            if ds and tbl:
                measures_lookup_by_ds_table.setdefault((ds, tbl, name), m)
    for t in results.get('Tables', []):
        tables_lookup[(t.get('workspace_id'), t.get('dataset_id'), t.get('Name'))] = t
        if t.get('dataset_id') and t.get('Name'):
            tables_lookup_by_ds.setdefault((t.get('dataset_id'), t.get('Name')), t)

    def _item_id_for(node_type, name, workspace_id=None, dataset_id=None, table_name=None, report_name=None, page_name=None, extra=None):
        """
        Compute the `Item.item_id` hash (without the "TYPE::" prefix) for a
        graph node. Mirrors the logic that produces `fabric_info_items.csv` so
        graph ids join 1:1 with Item rows.
        """
        if node_type == 'PB_TABLE':
            # Most-precise key first (workspace+dataset+name), then dataset-scoped,
            # then a column-derived fallback. Only fall back to a name-only lookup
            # last — global name-only would collapse same-named tables across
            # different datasets onto a single (wrong) hash.
            meta = (tables_lookup.get((workspace_id, dataset_id, name))
                    or (tables_lookup_by_ds.get((dataset_id, name)) if dataset_id else None)
                    or columns_lookup.get(name) or {})
            ds_id = dataset_id or meta.get('dataset_id')
            lineage = meta.get('lineage_tag')
            if lineage:
                return generate_custom_id(ds_id, lineage)
            return generate_custom_id(ds_id, name)
        if node_type == 'PB_COLUMN':
            # (dataset, table, name) > (table, name) > (dataset, name) > (name).
            # Dataset-scoped keys prevent cross-dataset name twins from
            # collapsing onto the first dataset's hash.
            meta = ((columns_lookup_by_ds_table.get((dataset_id, table_name, name)) if dataset_id and table_name else None)
                    or (columns_lookup_by_table.get((table_name, name)) if table_name else None)
                    or (columns_lookup_by_ds.get((dataset_id, name)) if dataset_id else None)
                    or columns_lookup.get(name) or {})
            ds_id = dataset_id or meta.get('dataset_id')
            tn = table_name or meta.get('Table_Name')
            lineage = meta.get('lineage_tag')
            if lineage:
                return generate_custom_id(ds_id, lineage)
            return generate_custom_id(ds_id, tn, name)
        if node_type == 'PB_MEASURE':
            meta = ((measures_lookup_by_ds_table.get((dataset_id, table_name, name)) if dataset_id and table_name else None)
                    or (measures_lookup_by_table.get((table_name, name)) if table_name else None)
                    or (measures_lookup_by_ds.get((dataset_id, name)) if dataset_id else None)
                    or measures_lookup.get(name) or {})
            ds_id = dataset_id or meta.get('dataset_id')
            tn = table_name or meta.get('Table_Name')
            lineage = meta.get('lineage_tag')
            if lineage:
                return generate_custom_id(ds_id, lineage)
            return generate_custom_id(ds_id, tn, name)
        if node_type == 'PB_REPORT':
            return generate_custom_id(workspace_id, name)
        if node_type == 'PB_PAGE':
            return generate_custom_id(workspace_id, report_name, name)
        if node_type == 'PB_VISUAL':
            return generate_custom_id(workspace_id, report_name, page_name, name)
        if node_type == 'PB_FIELD':
            return generate_custom_id(workspace_id, 'PB_FIELD', name)
        # Fallback (unknown node type)
        return generate_custom_id(node_type, workspace_id, name, extra)

    def make_node_id(node_type, name, **kwargs):
        """Return a globally-unique graph node id: "{TYPE}::{item_id_hash}"."""
        if not name:
            return None
        return f"{node_type}::{_item_id_for(node_type, name, **kwargs)}"

    # Pre-compute lookup sets to resolve field types
    # Build both exact-name and "last segment after dot" lookups for better matching
    measure_names = {m.get('Name') for m in results.get('Measures', [])}
    column_names = {c.get('Name') for c in results.get('Columns', [])}

    # Also build a lookup from the last dot-segment to the full name (for "Table.Column" style fields)
    # e.g. "DAT.date_actual" -> "DAT.date_actual", last segment "date_actual" -> "DAT.date_actual"
    measure_suffix_map = {}  # suffix -> full measure name
    for m in results.get('Measures', []):
        name = m.get('Name')
        if name:
            measure_suffix_map[name] = name
            suffix = name.split('.')[-1] if '.' in name else name
            if suffix not in measure_suffix_map:
                measure_suffix_map[suffix] = name

    column_suffix_map = {}  # suffix -> full column name
    for c in results.get('Columns', []):
        name = c.get('Name')
        if name:
            column_suffix_map[name] = name
            suffix = name.split('.')[-1] if '.' in name else name
            if suffix not in column_suffix_map:
                column_suffix_map[suffix] = name

    # Cache for resolve_field: same field name appears many times across visuals/pages
    _resolve_field_cache = {}

    def resolve_field(field_name):
        """Returns (resolved_name, field_type) for a field extracted from a visual."""
        if not field_name:
            return field_name, "PB_FIELD"
        cached = _resolve_field_cache.get(field_name)
        if cached is not None:
            return cached
        # Try exact match first
        if field_name in measure_names:
            result = (field_name, "PB_MEASURE")
        elif field_name in column_names:
            result = (field_name, "PB_COLUMN")
        else:
            result = None
            # Try last segment after last dot
            suffix = field_name.split('.')[-1] if '.' in field_name else field_name
            if suffix in measure_suffix_map:
                result = (measure_suffix_map[suffix], "PB_MEASURE")
            elif suffix in column_suffix_map:
                result = (column_suffix_map[suffix], "PB_COLUMN")
            else:
                # Try second-to-last segment (e.g. "D_Date (DAT).DAT.date_actual" -> "DAT.date_actual")
                parts = field_name.split('.')
                if len(parts) >= 2:
                    two_part = ".".join(parts[-2:])
                    if two_part in measure_names:
                        result = (two_part, "PB_MEASURE")
                    elif two_part in column_names:
                        result = (two_part, "PB_COLUMN")
            if result is None:
                result = (field_name, "PB_FIELD")
        _resolve_field_cache[field_name] = result
        return result

    # Edges below follow STANDARD DATA-LINEAGE CONVENTION:
    #   arrow points from PRODUCER (source of data) -> CONSUMER (what uses it).
    # So a building-block measure points INTO a derived measure; a field points INTO
    # the visual that uses it; a visual -> page -> report; a table -> its columns/measures.

    def _push_edge(src_name, src_type, tgt_name, tgt_type, workspace_id=None,
                   src_kwargs=None, tgt_kwargs=None, edge_kind='', lineage_type=''):
        """Append a graph edge with both human names and composite ids.

        ``edge_kind`` overrides the type-based kind classifier at load time for
        structural edges the classifier can't infer (e.g. 'join' for a FK→PK
        relationship, vs a DAX column→column dependency). ``lineage_type`` records
        how a column edge's target was derived. Both default to empty.
        """
        if not src_name or not tgt_name:
            return
        src_id = make_node_id(src_type, src_name, workspace_id=workspace_id, **(src_kwargs or {}))
        tgt_id = make_node_id(tgt_type, tgt_name, workspace_id=workspace_id, **(tgt_kwargs or {}))
        if not src_id or not tgt_id or src_id == tgt_id:
            return
        graph.append({
            "source_id": src_id, "source": src_name, "source_type": src_type,
            "target_id": tgt_id, "target": tgt_name, "target_type": tgt_type,
            "workspace_id": workspace_id or "",
            "edge_kind": edge_kind or "",
            "lineage_type": lineage_type or "",
        })

    # 1. Reports Lineage -> Graph (FIELD -> VISUAL -> PAGE -> REPORT)
    for r in results.get("Reports_Lineage", []):
        report_name = r.get("report_name")
        page_name = r.get("page_name")
        visual_id = r.get("visual_id")
        field_name = r.get("field_name")
        workspace_id = r.get("workspace_id")

        if report_name and page_name:
            # Page -> Report
            _push_edge(
                page_name, "PB_PAGE", report_name, "PB_REPORT",
                workspace_id=workspace_id,
                src_kwargs={"report_name": report_name},
            )

            if visual_id:
                # Visual -> Page
                _push_edge(
                    visual_id, "PB_VISUAL", page_name, "PB_PAGE",
                    workspace_id=workspace_id,
                    src_kwargs={"report_name": report_name, "page_name": page_name},
                    tgt_kwargs={"report_name": report_name},
                )

                # Field -> Visual (resolved)
                if field_name:
                    resolved_name, field_type = resolve_field(field_name)
                    src_kwargs = {}
                    if field_type in ("PB_COLUMN", "PB_MEASURE"):
                        meta = (columns_lookup if field_type == "PB_COLUMN" else measures_lookup).get(resolved_name, {})
                        src_kwargs = {
                            "dataset_id": meta.get("dataset_id"),
                            "table_name": meta.get("Table_Name"),
                        }
                    _push_edge(
                        resolved_name, field_type, visual_id, "PB_VISUAL",
                        workspace_id=workspace_id,
                        src_kwargs=src_kwargs,
                        tgt_kwargs={"report_name": report_name, "page_name": page_name},
                    )

    # 2. Dependencies -> Graph (ReferencedObject is the building block, Object is the consumer)
    #
    # DAX is dataset-scoped, so the consumer and producer always live in the
    # same dataset. We pass that dataset_id (added to the dep row by
    # process_semantic_model_dir) into _item_id_for so the lookup picks the
    # right meta when the same name exists in multiple datasets — without
    # this, name-only fallback collapses every twin onto the first one and
    # the duplicate ends up with no incoming edges.
    for d in results.get("Dependencies", []):
        consumer = d.get("Object")
        consumer_type = d.get("ObjectType")
        consumer_table = d.get("ObjectTable")
        producer = d.get("ReferencedObject")
        producer_type = d.get("ReferencedObjectType")
        producer_table = d.get("ReferencedTable")  # set only for 'Table'[Col] producers
        workspace_id = d.get("workspace_id")
        dataset_id = d.get("dataset_id")

        if consumer and producer and consumer_type in ["PB_MEASURE", "PB_COLUMN", "PB_TABLE"] and producer_type in ["PB_MEASURE", "PB_COLUMN", "PB_TABLE"]:
            def _dep_kwargs(name, node_type, hint_table):
                # Dataset-scoped lookup first, then fall back to name-only so
                # legacy callers still get *some* answer.
                meta = {}
                if node_type == "PB_COLUMN":
                    if dataset_id and hint_table:
                        meta = columns_lookup_by_ds_table.get((dataset_id, hint_table, name)) or {}
                    if not meta and dataset_id:
                        meta = columns_lookup_by_ds.get((dataset_id, name)) or {}
                    if not meta:
                        meta = columns_lookup.get(name, {})
                elif node_type == "PB_MEASURE":
                    if dataset_id and hint_table:
                        meta = measures_lookup_by_ds_table.get((dataset_id, hint_table, name)) or {}
                    if not meta and dataset_id:
                        meta = measures_lookup_by_ds.get((dataset_id, name)) or {}
                    if not meta:
                        meta = measures_lookup.get(name, {})
                elif node_type == "PB_TABLE":
                    if dataset_id:
                        meta = tables_lookup_by_ds.get((dataset_id, name)) or {}
                return {
                    "dataset_id": dataset_id or meta.get("dataset_id"),
                    "table_name": hint_table or meta.get("Table_Name"),
                }

            # A measure/column that references measures/columns in DAX is a
            # transformation of them (column-level data lineage). Table-level
            # deps stay plain edges classified by type.
            is_col_dep = (
                consumer_type in ("PB_MEASURE", "PB_COLUMN")
                and producer_type in ("PB_MEASURE", "PB_COLUMN")
            )
            _push_edge(
                producer, producer_type, consumer, consumer_type,
                workspace_id=workspace_id,
                src_kwargs=_dep_kwargs(producer, producer_type, producer_table),
                tgt_kwargs=_dep_kwargs(consumer, consumer_type, consumer_table),
                lineage_type='transformation' if is_col_dep else '',
            )

    # 3. Tables -> their Columns/Measures (a table produces the columns and measures defined on it)
    for c in results.get("Columns", []):
        _push_edge(
            c.get("Table_Name"), "PB_TABLE", c.get("Name"), "PB_COLUMN",
            workspace_id=c.get("workspace_id"),
            src_kwargs={"dataset_id": c.get("dataset_id")},
            tgt_kwargs={"dataset_id": c.get("dataset_id"), "table_name": c.get("Table_Name")},
        )
    for m in results.get("Measures", []):
        _push_edge(
            m.get("Table_Name"), "PB_TABLE", m.get("Name"), "PB_MEASURE",
            workspace_id=m.get("workspace_id"),
            src_kwargs={"dataset_id": m.get("dataset_id")},
            tgt_kwargs={"dataset_id": m.get("dataset_id"), "table_name": m.get("Table_Name")},
        )

    # 4. Relationships -> Graph (column-level joins between tables in a model).
    # Direction follows the TMDL spec: from many-side -> one-side (so the graph
    # arrow points from FK column to PK column, matching producer->consumer).
    for rel in results.get("Relationships", []):
        ws = rel.get("workspace_id")
        ds = rel.get("dataset_id")
        ft, fc = rel.get("from_table"), rel.get("from_column")
        tt, tc = rel.get("to_table"),   rel.get("to_column")
        if not (ft and fc and tt and tc):
            continue
        # Column -> column (the FK -> PK join). Tagged 'join' so the lineage UI
        # draws it as a structural relationship, distinct from data lineage —
        # overriding the type classifier (which would call it a 'column' edge).
        _push_edge(
            fc, "PB_COLUMN", tc, "PB_COLUMN",
            workspace_id=ws,
            src_kwargs={"dataset_id": ds, "table_name": ft},
            tgt_kwargs={"dataset_id": ds, "table_name": tt},
            edge_kind='join',
        )

    # Deduplicate graph on the (source_id, target_id) pair
    seen_edges = set()
    unique_graph = []
    for g in graph:
        key = (g["source_id"], g["target_id"])
        if key in seen_edges:
            continue
        seen_edges.add(key)
        unique_graph.append(g)
    results["Graph"] = unique_graph
    
    # ==========================================
    # BUILD ITEM_USAGE_RELATIONSHIPS (flat edge table)
    # ==========================================
    # Each row = one direct usage link between an item and something that references it.
    # Two sources of links:
    #   1. LINEAGE: column/measure -> report/page/visual (from report.json parsing)
    #   2. DEPENDENCY: column/measure/table -> measure/column/table (from DAX parsing)
    #
    # Schema:
    #   item_name, item_type, item_table_name, item_dataset_name, item_workspace_id
    #   ref_item_name, ref_item_type, ref_item_workspace_id
    #   relationship_source  (LINEAGE | DEPENDENCY)
    #
    # is_unused rules:
    #   COLUMN  -> unused if not referenced by any REPORT/PAGE/VISUAL (lineage) AND not referenced by any MEASURE/COLUMN (dependency)
    #   MEASURE -> unused if not referenced by any REPORT/PAGE/VISUAL (lineage) AND not referenced by any MEASURE (dependency)
    #   TABLE   -> unused if none of its columns/measures are used
    print("[ANALYSIS] Building item usage relationships table...")

    # Build lookup maps for item metadata
    column_meta = {c.get('Name'): c for c in results.get("Columns", [])}
    measure_meta = {m.get('Name'): m for m in results.get("Measures", [])}
    table_meta = {t.get('Name'): t for t in results.get("Tables", [])}

    item_usage_relationships = []

    # --- Source 1: LINEAGE links (field -> report/page/visual) ---
    # For each lineage row that has a resolved field, emit:
    #   (field, COLUMN/MEASURE) -> (report_name, REPORT)
    #   (field, COLUMN/MEASURE) -> (page_name, PAGE)
    #   (field, COLUMN/MEASURE) -> (visual_id, VISUAL)
    seen_lineage_links = set()
    for r in results.get("Reports_Lineage", []):
        field_name = r.get("field_name")
        if not field_name:
            continue
        resolved_name, field_type = resolve_field(field_name)
        if field_type == "PB_FIELD":
            continue  # couldn't resolve to a known column/measure

        report_name = r.get("report_name")
        page_name = r.get("page_name")
        visual_id = r.get("visual_id")
        ws_id = r.get("workspace_id")

        # Get item metadata
        if field_type == "PB_COLUMN":
            meta = column_meta.get(resolved_name, {})
        else:
            meta = measure_meta.get(resolved_name, {})

        item_ws = meta.get("workspace_id") or ws_id
        item_table = meta.get("Table_Name") or meta.get("table_name")
        item_dataset = meta.get("dataset_name")

        # Emit link to REPORT (deduplicated)
        if report_name:
            key = (resolved_name, field_type, report_name, "PB_REPORT")
            if key not in seen_lineage_links:
                seen_lineage_links.add(key)
                item_usage_relationships.append({
                    "item_name": resolved_name,
                    "item_type": field_type,
                    "item_table_name": item_table,
                    "item_dataset_name": item_dataset,
                    "item_workspace_id": item_ws,
                    "ref_item_name": report_name,
                    "ref_item_type": "PB_REPORT",
                    "ref_item_table_name": None,
                    "ref_item_dataset_name": None,
                    "ref_item_workspace_id": ws_id,
                    "relationship_source": "LINEAGE",
                })
        # Emit link to PAGE (deduplicated)
        if page_name:
            key = (resolved_name, field_type, page_name, "PB_PAGE")
            if key not in seen_lineage_links:
                seen_lineage_links.add(key)
                item_usage_relationships.append({
                    "item_name": resolved_name,
                    "item_type": field_type,
                    "item_table_name": item_table,
                    "item_dataset_name": item_dataset,
                    "item_workspace_id": item_ws,
                    "ref_item_name": page_name,
                    "ref_item_type": "PB_PAGE",
                    "ref_item_table_name": None,
                    "ref_item_dataset_name": None,
                    "ref_item_workspace_id": ws_id,
                    "relationship_source": "LINEAGE",
                })
        # Emit link to VISUAL (deduplicated)
        if visual_id:
            key = (resolved_name, field_type, visual_id, "PB_VISUAL")
            if key not in seen_lineage_links:
                seen_lineage_links.add(key)
                item_usage_relationships.append({
                    "item_name": resolved_name,
                    "item_type": field_type,
                    "item_table_name": item_table,
                    "item_dataset_name": item_dataset,
                    "item_workspace_id": item_ws,
                    "ref_item_name": visual_id,
                    "ref_item_type": "PB_VISUAL",
                    "ref_item_table_name": None,
                    "ref_item_dataset_name": None,
                    "ref_item_workspace_id": ws_id,
                    "relationship_source": "LINEAGE",
                })

    # --- Source 2: DEPENDENCY links (item -> referenced item) ---
    for d in results.get("Dependencies", []):
        obj_name = d.get("object") or d.get("Object")
        obj_type = d.get("objecttype") or d.get("ObjectType")
        ref_name = d.get("referencedobject") or d.get("ReferencedObject")
        ref_type = d.get("referencedobjecttype") or d.get("ReferencedObjectType")
        dep_ws = d.get("workspace_id")
        dep_dataset = d.get("dataset_name")

        if not obj_name or not ref_name:
            continue

        # Get item (source) metadata
        if obj_type == "PB_COLUMN":
            meta = column_meta.get(obj_name, {})
        elif obj_type == "PB_MEASURE":
            meta = measure_meta.get(obj_name, {})
        elif obj_type == "PB_TABLE":
            meta = table_meta.get(obj_name, {})
        else:
            meta = {}

        item_ws = meta.get("workspace_id") or dep_ws
        item_table = meta.get("Table_Name") or meta.get("table_name") or obj_name
        item_dataset = meta.get("dataset_name") or dep_dataset

        # Get ref (target) metadata
        if ref_type == "PB_COLUMN":
            ref_meta = column_meta.get(ref_name, {})
        elif ref_type == "PB_MEASURE":
            ref_meta = measure_meta.get(ref_name, {})
        elif ref_type == "PB_TABLE":
            ref_meta = table_meta.get(ref_name, {})
        else:
            ref_meta = {}

        ref_table = ref_meta.get("Table_Name") or ref_meta.get("table_name") or (ref_name if ref_type == "PB_TABLE" else None)
        ref_dataset = ref_meta.get("dataset_name") or dep_dataset

        item_usage_relationships.append({
            "item_name": obj_name,
            "item_type": obj_type,
            "item_table_name": item_table,
            "item_dataset_name": item_dataset,
            "item_workspace_id": item_ws,
            "ref_item_name": ref_name,
            "ref_item_type": ref_type,
            "ref_item_table_name": ref_table,
            "ref_item_dataset_name": ref_dataset,
            "ref_item_workspace_id": dep_ws,
            "relationship_source": "DEPENDENCY",
        })

    results["Item_Usage_Relationships"] = item_usage_relationships

    # ==========================================
    # IDENTIFY UNUSED ELEMENTS (based on relationships)
    # ==========================================
    print("[ANALYSIS] Finding unused tables, columns, and measures...")

    # Items used via LINEAGE (appear in a report/page/visual)
    used_in_lineage_measures = set()
    used_in_lineage_columns = set()

    # Items used via DEPENDENCY (referenced by another measure/column)
    used_in_dep_measures = set()
    used_in_dep_columns = set()
    used_tables = set()

    for row in item_usage_relationships:
        src = row["relationship_source"]
        iname = row["item_name"]
        itype = row["item_type"]
        rtype = row["ref_item_type"]

        if src == "LINEAGE" and rtype in ("PB_REPORT", "PB_PAGE", "PB_VISUAL"):
            if itype == "PB_MEASURE":
                used_in_lineage_measures.add(iname)
            elif itype == "PB_COLUMN":
                used_in_lineage_columns.add(iname)
        elif src == "DEPENDENCY":
            ref_name = row["ref_item_name"]
            if rtype == "PB_MEASURE":
                used_in_dep_measures.add(ref_name)
            elif rtype == "PB_COLUMN":
                used_in_dep_columns.add(ref_name)
            elif rtype == "PB_TABLE":
                used_tables.add(ref_name)

    # A column is used if it appears in lineage OR is referenced by a measure/column
    used_columns = used_in_lineage_columns | used_in_dep_columns
    # A measure is used if it appears in lineage OR is referenced by another measure
    used_measures = used_in_lineage_measures | used_in_dep_measures

    # Map back to tables
    table_of_column = {c.get('Name'): c.get('Table_Name') for c in results.get("Columns", [])}
    table_of_measure = {m.get('Name'): m.get('Table_Name') for m in results.get("Measures", [])}

    for c in used_columns:
        if c in table_of_column: used_tables.add(table_of_column[c])
    for m in used_measures:
        if m in table_of_measure: used_tables.add(table_of_measure[m])

    # ==========================================
    # COMPUTE USAGE STATS + downstream REPORT ids (topological propagation)
    # ==========================================
    # We use composite ids (TYPE::hash) from unique_graph so we can:
    #   1. Count connected_reports / pages / visuals / measures / columns / tables per node
    #   2. Collect downstream REPORT composite ids for connected_reports_json
    # Uses topological sort + reverse propagation: O(N+E) instead of O(N*(N+E)).
    print("[ANALYSIS] Computing usage stats and downstream reports (topological propagation)...")

    # Build forward adjacency using composite ids
    fwd = collections.defaultdict(list)  # source_id -> [(target_id, target_type)]
    for g in unique_graph:
        fwd[g['source_id']].append((g['target_id'], g['target_type']))

    # Also build a name lookup: composite_id -> (name, type) for stat counting
    id_to_name_type = {}
    for g in unique_graph:
        id_to_name_type[g['source_id']] = (g['source'], g['source_type'])
        id_to_name_type[g['target_id']] = (g['target'], g['target_type'])

    # Per-node stats keyed by composite id
    node_stats = collections.defaultdict(lambda: {
        'connected_reports': set(),
        'connected_report_pages': set(),
        'connected_visuals': set(),
        'connected_measures': set(),
        'connected_columns': set(),
        'connected_tables': set(),
        'downstream_report_ids': set(),  # composite REPORT ids for connected_reports_json
    })

    all_ids = set(id_to_name_type.keys())

    # --- Topological sort (Kahn's algorithm) ---
    in_degree = {nid: 0 for nid in all_ids}
    for src_id, children in fwd.items():
        for tgt_id, _ in children:
            if tgt_id in in_degree:
                in_degree[tgt_id] += 1

    topo_queue = collections.deque(nid for nid in all_ids if in_degree[nid] == 0)
    topo_order = []
    while topo_queue:
        nid = topo_queue.popleft()
        topo_order.append(nid)
        for child_id, _ in fwd.get(nid, []):
            if child_id in in_degree:
                in_degree[child_id] -= 1
                if in_degree[child_id] == 0:
                    topo_queue.append(child_id)

    topo_set = set(topo_order)

    # --- Propagate stats in reverse topological order (sinks first, sources last) ---
    # Each node's downstream stats = direct children contributions + union of children's
    # transitive stats. Children are always processed before parents in reverse topo order.
    for nid in reversed(topo_order):
        stats = node_stats[nid]
        for child_id, child_type in fwd.get(nid, []):
            if child_id == nid:
                continue  # skip self-loops
            # Add the direct child's contribution
            child_name = id_to_name_type.get(child_id, (child_id, child_type))[0]
            if child_type == 'PB_REPORT':
                stats['connected_reports'].add(child_name)
                stats['downstream_report_ids'].add(child_id)
            elif child_type == 'PB_PAGE':
                stats['connected_report_pages'].add(child_name)
            elif child_type == 'PB_VISUAL':
                stats['connected_visuals'].add(child_name)
            elif child_type == 'PB_MEASURE':
                stats['connected_measures'].add(child_name)
            elif child_type == 'PB_COLUMN':
                stats['connected_columns'].add(child_name)
            elif child_type == 'PB_TABLE':
                stats['connected_tables'].add(child_name)
            # Merge child's already-computed transitive downstream stats
            child_stats = node_stats[child_id]
            stats['connected_reports'] |= child_stats['connected_reports']
            stats['downstream_report_ids'] |= child_stats['downstream_report_ids']
            stats['connected_report_pages'] |= child_stats['connected_report_pages']
            stats['connected_visuals'] |= child_stats['connected_visuals']
            stats['connected_measures'] |= child_stats['connected_measures']
            stats['connected_columns'] |= child_stats['connected_columns']
            stats['connected_tables'] |= child_stats['connected_tables']

    # --- Fallback: BFS for any nodes involved in cycles (not in topological order) ---
    cycle_nodes = all_ids - topo_set
    if cycle_nodes:
        for root_id in cycle_nodes:
            visited = set()
            queue = collections.deque([root_id])
            while queue:
                cur_id = queue.popleft()
                if cur_id in visited:
                    continue
                visited.add(cur_id)
                for child_id, child_type in fwd.get(cur_id, []):
                    if child_id not in visited:
                        if cur_id != root_id or child_id != root_id:
                            child_name = id_to_name_type.get(child_id, (child_id, child_type))[0]
                            if child_type == 'PB_REPORT':
                                node_stats[root_id]['connected_reports'].add(child_name)
                                node_stats[root_id]['downstream_report_ids'].add(child_id)
                            elif child_type == 'PB_PAGE':
                                node_stats[root_id]['connected_report_pages'].add(child_name)
                            elif child_type == 'PB_VISUAL':
                                node_stats[root_id]['connected_visuals'].add(child_name)
                            elif child_type == 'PB_MEASURE':
                                node_stats[root_id]['connected_measures'].add(child_name)
                            elif child_type == 'PB_COLUMN':
                                node_stats[root_id]['connected_columns'].add(child_name)
                            elif child_type == 'PB_TABLE':
                                node_stats[root_id]['connected_tables'].add(child_name)
                        queue.append(child_id)

    def get_stats_counts(composite_id):
        s = node_stats.get(composite_id, {})
        return {
            'connected_reports': len(s.get('connected_reports', set())),
            'connected_report_pages': len(s.get('connected_report_pages', set())),
            'connected_visuals': len(s.get('connected_visuals', set())),
            'connected_measures': len(s.get('connected_measures', set())),
            'connected_columns': len(s.get('connected_columns', set())),
            'connected_tables': len(s.get('connected_tables', set())),
        }

    def get_downstream_report_ids(composite_id):
        return node_stats.get(composite_id, {}).get('downstream_report_ids', set())

    # Build usage_stats_items using composite ids for lookup
    # We need a mapping from (name, type, workspace_id) -> composite_id for the join
    # Build it from the graph edges
    name_type_ws_to_id = {}
    for g in unique_graph:
        key_s = (g['source'], g['source_type'], g.get('workspace_id', ''))
        key_t = (g['target'], g['target_type'], g.get('workspace_id', ''))
        name_type_ws_to_id.setdefault(key_s, g['source_id'])
        name_type_ws_to_id.setdefault(key_t, g['target_id'])

    def _composite_id_for(name, item_type, workspace_id=''):
        return name_type_ws_to_id.get((name, item_type, workspace_id or ''))

    usage_stats_items = []

    for c in results.get("Columns", []):
        is_unused = c.get("Name") not in used_columns
        cid = _composite_id_for(c.get("Name"), "PB_COLUMN", c.get("workspace_id", ""))
        stats = get_stats_counts(cid) if cid else {
            'connected_reports': 0, 'connected_report_pages': 0, 'connected_visuals': 0,
            'connected_measures': 0, 'connected_columns': 0, 'connected_tables': 0
        }
        item_data = {
            "item_name": c.get("Name"), "item_type": "PB_COLUMN",
            "table_name": c.get("Table_Name"), "dataset_name": c.get("dataset_name"),
            "workspace_id": c.get("workspace_id"), "is_unused": is_unused,
        }
        item_data.update(stats)
        usage_stats_items.append(item_data)

    for m in results.get("Measures", []):
        is_unused = m.get("Name") not in used_measures
        cid = _composite_id_for(m.get("Name"), "PB_MEASURE", m.get("workspace_id", ""))
        stats = get_stats_counts(cid) if cid else {
            'connected_reports': 0, 'connected_report_pages': 0, 'connected_visuals': 0,
            'connected_measures': 0, 'connected_columns': 0, 'connected_tables': 0
        }
        item_data = {
            "item_name": m.get("Name"), "item_type": "PB_MEASURE",
            "table_name": m.get("Table_Name"), "dataset_name": m.get("dataset_name"),
            "workspace_id": m.get("workspace_id"), "is_unused": is_unused,
        }
        item_data.update(stats)
        usage_stats_items.append(item_data)

    for t in results.get("Tables", []):
        is_unused = t.get("Name") not in used_tables
        cid = _composite_id_for(t.get("Name"), "PB_TABLE", t.get("workspace_id", ""))
        stats = get_stats_counts(cid) if cid else {
            'connected_reports': 0, 'connected_report_pages': 0, 'connected_visuals': 0,
            'connected_measures': 0, 'connected_columns': 0, 'connected_tables': 0
        }
        item_data = {
            "item_name": t.get("Name"), "item_type": "PB_TABLE",
            "table_name": t.get("Name"), "dataset_name": t.get("dataset_name"),
            "workspace_id": t.get("workspace_id"), "is_unused": is_unused,
        }
        item_data.update(stats)
        usage_stats_items.append(item_data)

    results["Usage_Stats"] = usage_stats_items

    # ==========================================
    # BUILD RELATIONSHIP INDEX (per-table / per-column)
    # ==========================================
    # For every TABLE and COLUMN that participates in any relationship in its
    # dataset, we attach:
    #   - is_related (bool)
    #   - relationships_json (list of dicts describing each link)
    #
    # Tables get one entry per relationship they're a side of.
    # Columns get one entry per relationship they're a side of.
    # Each entry is from the perspective of the item:
    #   {role, this_table, this_column, other_table, other_column,
    #    cardinality, cross_filter, is_active, dataset_id}
    print("[ANALYSIS] Indexing semantic-model relationships per item...")

    rel_index_table = collections.defaultdict(list)   # (dataset_id, table_name) -> list[dict]
    rel_index_column = collections.defaultdict(list)  # (dataset_id, table_name, column_name) -> list[dict]
    seen_per_table = collections.defaultdict(set)
    seen_per_column = collections.defaultdict(set)

    for rel in results.get("Relationships", []):
        ds = rel.get("dataset_id")
        ft, fc = rel.get("from_table"), rel.get("from_column")
        tt, tc = rel.get("to_table"),   rel.get("to_column")
        if not (ft and fc and tt and tc):
            continue
        from_card = rel.get("from_cardinality") or "many"
        to_card = rel.get("to_cardinality") or "one"
        cross = rel.get("cross_filtering") or "single"
        active = rel.get("is_active", True)

        # FROM-side perspective
        from_entry = {
            "role": "from",
            "this_table": ft, "this_column": fc,
            "other_table": tt, "other_column": tc,
            "cardinality": from_card,
            "other_cardinality": to_card,
            "cross_filter": cross,
            "is_active": active,
        }
        # TO-side perspective
        to_entry = {
            "role": "to",
            "this_table": tt, "this_column": tc,
            "other_table": ft, "other_column": fc,
            "cardinality": to_card,
            "other_cardinality": from_card,
            "cross_filter": cross,
            "is_active": active,
        }

        # Tables: dedupe on (other_table, this_column, other_column, is_active)
        # so two FK columns from the same table to the same target collapse cleanly.
        t_from_key = (tt, fc, tc, active)
        if t_from_key not in seen_per_table[(ds, ft)]:
            seen_per_table[(ds, ft)].add(t_from_key)
            rel_index_table[(ds, ft)].append(from_entry)
        t_to_key = (ft, tc, fc, active)
        if t_to_key not in seen_per_table[(ds, tt)]:
            seen_per_table[(ds, tt)].add(t_to_key)
            rel_index_table[(ds, tt)].append(to_entry)

        # Columns: dedupe on (other_table, other_column, is_active)
        c_from_key = (tt, tc, active)
        if c_from_key not in seen_per_column[(ds, ft, fc)]:
            seen_per_column[(ds, ft, fc)].add(c_from_key)
            rel_index_column[(ds, ft, fc)].append(from_entry)
        c_to_key = (ft, fc, active)
        if c_to_key not in seen_per_column[(ds, tt, tc)]:
            seen_per_column[(ds, tt, tc)].add(c_to_key)
            rel_index_column[(ds, tt, tc)].append(to_entry)

    print("--- [SAVING] Saving Results ---")
    
    # Ensure data directory exists
    data_dir = os.path.join(os.path.dirname(__file__), "data")
    os.makedirs(data_dir, exist_ok=True)

    # Keep DataFrames in memory for the Items union step
    saved_dfs = {}

    for category, rows in results.items():
        if rows:
            df = pd.DataFrame(rows)
            df.columns = df.columns.str.lower()
            
            # Apply Custom IDs for linkage. Use lineage_tag if present, else fallback to hashing.
            # ID format: hash(dataset_id + lineage_tag or table_name + name)
            if category == "Tables":
                df['custom_table_id'] = df.apply(lambda r: get_item_id(r, "Tables"), axis=1)
            elif category == "Columns":
                df['custom_column_id'] = df.apply(lambda r: get_item_id(r, "Columns"), axis=1)
            elif category == "Measures":
                df['custom_measure_id'] = df.apply(lambda r: get_item_id(r, "Measures"), axis=1)
            elif category in ["Reports_Lineage", "Reports_Stats"]:
                # FIX: custom_report_id now uses workspace_id (stable UUID) + report_name
                df['custom_report_id'] = df.apply(lambda r: generate_custom_id(r.get('workspace_id'), r.get('report_name')), axis=1)
            
            # Clean up missing data strings before saving to CSV
            df = df.fillna('')
            df = df.replace({'nan': '', 'NaN': '', 'None': ''})

            saved_dfs[category] = df
            output_path = os.path.join(data_dir, f"fabric_info_{category.lower()}.csv")
            df.to_csv(output_path, index=False, encoding='utf-8-sig')
            print(f"[SUCCESS] Saved: {output_path} ({len(df)} rows)")
        else:
            print(f"[WARNING] No data to save for {category}")

    # ==========================================
    # BUILD fabric_info_items (UNION of Workspaces + Tables + Columns + Measures)
    # LEFT JOIN with Usage_Stats for usage metrics
    # ==========================================
    print("[ITEMS] Building fabric_info_items unified catalog...")

    ITEMS_COLUMNS = [
        'item_id',           # custom_*_id or workspace_id
        'lineage_tag',       # lineage tag for debugging
        'item_name',         # display name of the item
        'item_type',         # WORKSPACE | TABLE | COLUMN | MEASURE | REPORT | PAGE | VISUAL
        'item_service',      # e.g. powerbi
        'description',       # description if available
        'workspace_id',
        'workspace_name',    # joined from workspaces
        'dataset_id',        # semantic model ID
        'dataset_name',      # semantic model name (null for workspaces)
        'table_name',        # parent table (null for workspaces/tables)
        # Column-specific
        'datatype',
        'column_type',       # calculated / data
        'expression',        # DAX expression (columns + measures)
        # Measure-specific
        'formatstring',
        # PowerBI TABLE: BigQuery FQN extracted from the M-query partition.
        # Used by the dbt ↔ PowerBI bridge as the preferred join key.
        'bq_project',
        'bq_schema',
        'bq_source_name',
        # Web URL
        'web_url',
        # Usage stats (LEFT JOIN)
        'is_unused',
        'connected_reports',
        'connected_report_pages',
        'connected_visuals',
        'connected_measures',
        'connected_columns',
        'connected_tables',
        'connected_reports_json',   # JSON array of {id, name, url} for MEASURE/COLUMN
        # Semantic-model relationships (TABLE / COLUMN only).
        # is_related = participates in any relationship in its dataset.
        # relationships_json = list of {role, this_table, this_column,
        #   other_table, other_column, cardinality, other_cardinality,
        #   cross_filter, is_active}
        'is_related',
        'relationships_json',
    ]

    ws_df = saved_dfs.get('Workspaces', pd.DataFrame())
    tbl_df = saved_dfs.get('Tables', pd.DataFrame())
    col_df = saved_dfs.get('Columns', pd.DataFrame())
    meas_df = saved_dfs.get('Measures', pd.DataFrame())
    usage_df = saved_dfs.get('Usage_Stats', pd.DataFrame())

    # Build workspace name lookup
    ws_name_lookup = {}
    if not ws_df.empty:
        for _, row in ws_df.iterrows():
            ws_name_lookup[row.get('workspace_id', '')] = row.get('workspace_name', '')

    # Build report_meta_lookup from Reports_Stats for connected_reports_json
    # Keys: composite id "PB_REPORT::<hash>", Values: {"id": hash, "name": ..., "url": ...}
    _report_meta_lookup = {}
    _rep_stats_for_lookup = saved_dfs.get('Reports_Stats', pd.DataFrame())
    if not _rep_stats_for_lookup.empty:
        for _, _r in _rep_stats_for_lookup.iterrows():
            _rid = _r.get('custom_report_id')
            if not _rid:
                continue
            _composite = f"PB_REPORT::{_rid}"
            _report_meta_lookup[_composite] = {
                'id': _rid,
                'name': _r.get('report_name', ''),
                'url': _r.get('web_url', '') or '',
            }

    def _get_connected_reports_json(item_id, item_type):
        """Return sorted list of report dicts for MEASURE/COLUMN items, else []."""
        if item_type not in ('PB_MEASURE', 'PB_COLUMN'):
            return []
        composite_id = f"{item_type}::{item_id}"
        report_ids = get_downstream_report_ids(composite_id)
        result = []
        for rid in report_ids:
            meta = _report_meta_lookup.get(rid)
            if meta:
                result.append(meta)
        return sorted(result, key=lambda r: r.get('name', ''))

    item_rows = []

    # 1. WORKSPACES
    # NOTE: workspaces are not associated with a dataset or a report, so both
    # dataset_name and table_name are intentionally None here.
    for _, row in ws_df.iterrows():
        item_rows.append({
            'item_id': row.get('workspace_id'),
            'lineage_tag': None,
            'item_name': row.get('workspace_name'),
            'item_type': 'PB_WORKSPACE',
            'item_service': 'powerbi',
            'description': None,
            'workspace_id': row.get('workspace_id'),
            'workspace_name': row.get('workspace_name'),
            'dataset_id': None,
            'dataset_name': None,
            'table_name': None,
            'datatype': None,
            'column_type': None,
            'expression': None,
            'formatstring': None,
        })

    # 2. TABLES
    # Build a lookup of BigQuery FQN keyed by (dataset_id, table_name) so we
    # can persist (bq_project, bq_schema, bq_source_name) on every TABLE item.
    # The Sources DF was lower-cased on save, so 'Table_Name' → 'table_name'.
    src_df = saved_dfs.get('Sources', pd.DataFrame())
    bq_lookup = {}
    if not src_df.empty:
        for _, sr in src_df.iterrows():
            ds = sr.get('dataset_id')
            tname = sr.get('table_name')
            if not ds or not tname:
                continue
            bq_lookup[(ds, tname)] = (
                sr.get('bq_project') or None,
                sr.get('bq_schema') or None,
                sr.get('bq_source_name') or None,
            )

    for _, row in tbl_df.iterrows():
        ws_id = row.get('workspace_id')
        bq_proj, bq_sch, bq_src = bq_lookup.get(
            (row.get('dataset_id'), row.get('name')), (None, None, None)
        )
        tbl_rels = rel_index_table.get((row.get('dataset_id'), row.get('name')), [])
        item_rows.append({
            'item_id': row.get('custom_table_id'),
            'lineage_tag': row.get('lineage_tag'),
            'item_name': row.get('name'),
            'item_type': 'PB_TABLE',
            'item_service': 'powerbi',
            'description': row.get('description'),
            'workspace_id': ws_id,
            'workspace_name': ws_name_lookup.get(ws_id, ''),
            'dataset_id': row.get('dataset_id'),
            'dataset_name': row.get('dataset_name'),
            'table_name': None,
            'datatype': None,
            'column_type': None,
            'expression': None,
            'formatstring': None,
            'bq_project': bq_proj,
            'bq_schema': bq_sch,
            'bq_source_name': bq_src,
            'is_related': bool(tbl_rels),
            'relationships_json': tbl_rels,
        })

    # 3. COLUMNS
    for _, row in col_df.iterrows():
        ws_id = row.get('workspace_id')
        col_item_id = row.get('custom_column_id')
        col_rels = rel_index_column.get(
            (row.get('dataset_id'), row.get('table_name'), row.get('name')), []
        )
        item_rows.append({
            'item_id': col_item_id,
            'lineage_tag': row.get('lineage_tag'),
            'item_name': row.get('name'),
            'item_type': 'PB_COLUMN',
            'item_service': 'powerbi',
            'description': row.get('description'),
            'workspace_id': ws_id,
            'workspace_name': ws_name_lookup.get(ws_id, ''),
            'dataset_id': row.get('dataset_id'),
            'dataset_name': row.get('dataset_name'),
            'table_name': row.get('table_name'),
            'datatype': row.get('datatype'),
            'column_type': row.get('type'),
            'expression': row.get('expression'),
            'formatstring': None,
            'connected_reports_json': _get_connected_reports_json(col_item_id, 'PB_COLUMN'),
            'is_related': bool(col_rels),
            'relationships_json': col_rels,
        })

    # 4. MEASURES
    for _, row in meas_df.iterrows():
        ws_id = row.get('workspace_id')
        meas_item_id = row.get('custom_measure_id')
        item_rows.append({
            'item_id': meas_item_id,
            'lineage_tag': row.get('lineage_tag'),
            'item_name': row.get('name'),
            'item_type': 'PB_MEASURE',
            'item_service': 'powerbi',
            'description': row.get('description'),
            'workspace_id': ws_id,
            'workspace_name': ws_name_lookup.get(ws_id, ''),
            'dataset_id': row.get('dataset_id'),
            'dataset_name': row.get('dataset_name'),
            'table_name': row.get('table_name'),
            'datatype': None,
            'column_type': None,
            'expression': row.get('expression'),
            'formatstring': row.get('formatstring'),
            'connected_reports_json': _get_connected_reports_json(meas_item_id, 'PB_MEASURE'),
        })

    # 5. REPORTS
    rep_stats_df = saved_dfs.get('Reports_Stats', pd.DataFrame())
    for _, row in rep_stats_df.iterrows():
        ws_id = row.get('workspace_id')
        # Use web_url from metadata if available, otherwise construct from workspace_id + report item_id
        report_web_url = row.get('web_url') or ''
        if not report_web_url and ws_id:
            # Construct standard Power BI report URL from workspace_id
            # The report item_id is stored in item_metadata.json as 'id'
            report_item_id = row.get('report_id') or row.get('item_id') or ''
            if report_item_id:
                report_web_url = f'https://app.powerbi.com/groups/{ws_id}/reports/{report_item_id}'
        item_rows.append({
            'item_id': row.get('custom_report_id'),
            'lineage_tag': None,
            'item_name': row.get('report_name'),
            'item_type': 'PB_REPORT',
            'item_service': 'powerbi',
            'description': None,
            'workspace_id': ws_id,
            'workspace_name': ws_name_lookup.get(ws_id, ''),
            'dataset_id': None,
            'dataset_name': None,
            'table_name': None,
            'datatype': None,
            'column_type': None,
            'expression': None,
            'formatstring': None,
            'web_url': report_web_url or None,
            'connected_report_pages': row.get('total_pages', 0),
            'connected_visuals': row.get('total_visuals', 0),
        })

    # 6. PAGES and VISUALS
    rep_lin_df = saved_dfs.get('Reports_Lineage', pd.DataFrame())
    seen_pages = set()
    seen_visuals = set()
    if not rep_lin_df.empty:
        for _, row in rep_lin_df.iterrows():
            ws_id = row.get('workspace_id')
            ws_name = ws_name_lookup.get(ws_id, '')
            page_name = row.get('page_name')
            visual_id = row.get('visual_id')
            
            if page_name and page_name not in seen_pages:
                seen_pages.add(page_name)
                item_rows.append({
                    'item_id': generate_custom_id(ws_id, row.get('report_name'), page_name),
                    'lineage_tag': None,
                    'item_name': page_name,
                    'item_type': 'PB_PAGE',
                    'item_service': 'powerbi',
                    'description': None,
                    'workspace_id': ws_id,
                    'workspace_name': ws_name,
                    'dataset_id': None,
                    'dataset_name': row.get('report_name'),
                    'table_name': None,
                    'datatype': None,
                    'column_type': None,
                    'expression': None,
                    'formatstring': None,
                })
            
            if visual_id and visual_id not in seen_visuals:
                seen_visuals.add(visual_id)
                item_rows.append({
                    'item_id': generate_custom_id(ws_id, row.get('report_name'), page_name, visual_id),
                    'lineage_tag': None,
                    'item_name': visual_id,
                    'item_type': 'PB_VISUAL',
                    'item_service': 'powerbi',
                    'description': None,
                    'workspace_id': ws_id,
                    'workspace_name': ws_name,
                    'dataset_id': None,
                    'dataset_name': row.get('report_name'),
                    'table_name': page_name,
                    'datatype': None,
                    'column_type': None,
                    'expression': None,
                    'formatstring': None,
                })

    # 7. FIELDS (unresolved field references seen in visuals)
    # These are field names that appeared in a visual's config but could not be
    # matched to any known COLUMN or MEASURE in the semantic models. We still
    # create Items for them so the graph UI can show a consistent modal for
    # every node and the dictionary / catalog stays complete.
    seen_fields = set()
    if not rep_lin_df.empty:
        for _, row in rep_lin_df.iterrows():
            field_name = row.get('field_name')
            if not field_name:
                continue
            resolved_name, field_type = resolve_field(field_name)
            if field_type != 'PB_FIELD':
                # Already represented as a COLUMN / MEASURE item
                continue
            ws_id = row.get('workspace_id')
            ws_name = ws_name_lookup.get(ws_id, '')
            # Dedup by (workspace_id, field_name) so the same unresolved field
            # across multiple visuals / pages collapses to a single Item.
            key = (ws_id, field_name)
            if key in seen_fields:
                continue
            seen_fields.add(key)
            item_rows.append({
                'item_id': generate_custom_id(ws_id, 'PB_FIELD', field_name),
                'lineage_tag': None,
                'item_name': field_name,
                'item_type': 'PB_FIELD',
                'item_service': 'powerbi',
                'description': None,
                'workspace_id': ws_id,
                'workspace_name': ws_name,
                'dataset_id': None,
                'dataset_name': None,
                'table_name': None,
                'datatype': None,
                'column_type': None,
                'expression': None,
                'formatstring': None,
            })

    if item_rows:
        items_df = pd.DataFrame(item_rows)

        # UPDATE with usage_stats on (item_name, item_type, workspace_id)
        if not usage_df.empty:
            usage_cols = ['item_name', 'item_type', 'workspace_id',
                          'is_unused', 'connected_reports', 'connected_report_pages',
                          'connected_visuals', 'connected_measures', 'connected_columns', 'connected_tables']
            usage_subset = usage_df[[c for c in usage_cols if c in usage_df.columns]].copy()
            
            # Normalise join keys
            for c in ['item_name', 'item_type', 'workspace_id']:
                if c in usage_subset.columns:
                    usage_subset[c] = usage_subset[c].astype(str).str.strip()
                if c in items_df.columns:
                    items_df[c] = items_df[c].astype(str).str.strip()
            
            # Drop duplicates before setting index to prevent ValueError
            idx_cols = ['item_name', 'item_type', 'workspace_id']
            usage_subset = usage_subset.drop_duplicates(subset=idx_cols, keep='last')

            # Create a MultiIndex for updating
            items_df = items_df.set_index(idx_cols).sort_index()
            usage_subset = usage_subset.set_index(idx_cols).sort_index()
            
            # Ensure all columns exist in items_df so update can populate them
            for col in usage_subset.columns:
                if col not in items_df.columns:
                    items_df[col] = pd.NA
                    
            # Use update to overwrite NaN or apply values without overwriting existing numeric counts from Reports
            items_df.update(usage_subset, overwrite=False)
            items_df = items_df.reset_index()

        # Reorder to canonical column order (keep any extra cols at end)
        ordered = [c for c in ITEMS_COLUMNS if c in items_df.columns]
        extra = [c for c in items_df.columns if c not in ordered]
        items_df = items_df[ordered + extra]

        # JSON-encode connected_reports_json before CSV write so each cell is
        # a valid JSON string (not Python repr of a list).
        if 'connected_reports_json' in items_df.columns:
            items_df['connected_reports_json'] = items_df['connected_reports_json'].apply(
                lambda v: json.dumps(v if isinstance(v, list) else [], ensure_ascii=False)
            )

        # Same JSON encoding for relationships_json so the CSV cell is a
        # valid JSON string (not Python list repr).
        if 'relationships_json' in items_df.columns:
            items_df['relationships_json'] = items_df['relationships_json'].apply(
                lambda v: json.dumps(v if isinstance(v, list) else [], ensure_ascii=False)
            )

        # Clean up missing data strings before saving to CSV
        items_df = items_df.fillna('')
        items_df = items_df.replace({'nan': '', 'NaN': '', 'None': ''})

        # Ensure no duplicate item_id
        duplicate_mask = items_df.duplicated(subset=['item_id'], keep=False)
        if duplicate_mask.any():
            dup_ids = items_df[duplicate_mask]['item_id'].unique()
            print(f"[WARNING] Found {len(dup_ids)} duplicated item_ids. Keeping the last occurrence. Sample duplicate IDs: {list(dup_ids)[:10]}")
            items_df = items_df.drop_duplicates(subset=['item_id'], keep='last')

        items_path = os.path.join(data_dir, "fabric_info_items.csv")
        items_df.to_csv(items_path, index=False, encoding='utf-8-sig')
        print(f"[SUCCESS] Saved: {items_path} ({len(items_df)} rows)")
    else:
        print("[WARNING] No data to save for Items")

if __name__ == "__main__":
    main()
