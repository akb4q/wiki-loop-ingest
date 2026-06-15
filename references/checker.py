#!/usr/bin/env python3
"""Deterministic Checker for Wiki Loop Ingest.
No LLM — pure filesystem + text parsing.

All vault-specific assumptions (paths, directory names, tag-extraction regex,
required frontmatter fields, iCloud git fallback, stale window) are read from
the config file and fall back to sensible defaults, so the checker can be
pointed at any LLM-wiki layout without editing this file.
"""

import json, os, sys, subprocess, re, datetime

CONFIG_PATH = os.environ.get("WIKI_INGEST_CONFIG",
                             os.path.expanduser("~/.hermes/ingestion/config.json"))

# Defaults reproduce the original hardcoded behavior. Override any key in config.json.
DEFAULTS = {
    "vault_root": "",
    "schema_file": "wiki/SCHEMA.md",
    "index_file": "wiki/index.md",
    "log_file": "wiki/log.md",
    # dir key -> path relative to vault_root. The key doubles as the index stat
    # name (anchor `^stat-<key>`). Add/rename freely for your own layout.
    "dirs": {
        "sources": "wiki/sources",
        "concepts": "wiki/concepts",
        "entities": "wiki/entities",
        "comparisons": "wiki/comparisons",
        "queries": "wiki/queries",
    },
    # which dir keys hold linkable pages (wikilink resolution + dup detection)
    "artifact_dir_keys": ["sources", "concepts", "entities"],
    # which dir keys are scanned for orphan pages
    "orphan_dir_keys": ["concepts", "entities"],
    # which dir keys are scanned for stale content / contradictions
    "page_dir_keys": ["concepts", "entities", "comparisons"],
    # required frontmatter fields on generated wiki artifacts (sources/concepts/entities)
    "required_frontmatter": ["title", "tags"],
    # required frontmatter fields on the raw input file (uses singular `source` = URL)
    "source_required_frontmatter": ["title", "source", "tags"],
    # regex extracting valid tag slugs from the schema file (group 1 = slug).
    # Matches a backticked slug followed by " / gloss": `slug` / 中文.
    # The closing backtick is required — without it the pattern matches inline
    # code paths like `raw/articles/...` instead of tag definitions.
    "schema_tag_regex": r"`([a-z][a-z0-9-]*)`\s*/",
    # regex a single tag must match to be a well-formed slug
    "valid_slug_regex": r"^[a-z][a-z0-9-]*$",
    "icloud_git_fallback": True,
    "stale_days": 90,
}


def load_config(path=CONFIG_PATH):
    cfg = dict(DEFAULTS)
    try:
        with open(path) as f:
            user = json.load(f)
        for k, v in user.items():
            if k == "dirs" and isinstance(v, dict):
                merged = dict(DEFAULTS["dirs"]); merged.update(v); cfg["dirs"] = merged
            else:
                cfg[k] = v
    except Exception:
        pass
    if not cfg.get("vault_root"):
        cfg["vault_root"] = os.environ.get("WIKI_VAULT_ROOT", "")
    return cfg


CFG = load_config()
VAULT = CFG["vault_root"]

FRONTMATTER_RE = re.compile(r"^\ufeff?(?:[ \t]*\r?\n)*---\r?\n(.*?)\r?\n---(?:\r?\n|$)", re.DOTALL)
INLINE_TAGS_RE = re.compile(r"(?m)^[ \t]*tags[ \t]*:[ \t]*\[(.*?)\][ \t]*$")
LIST_TAGS_RE = re.compile(
    r"(?ms)^[ \t]*tags[ \t]*:[ \t]*(?:\r?\n)((?:[ \t]+-[ \t]*[^\r\n]+(?:\r?\n|$))+)"
)
TAG_TOKEN_RE = re.compile(r"([a-z][a-z0-9-]*)")
SCHEMA_TAG_RE = re.compile(CFG["schema_tag_regex"])
# Valid slug: configurable. Default rejects bilingual ("x / 中文"),
# multi-word ("AI agent"), uppercase ("AI"), and CJK-only tags.
VALID_SLUG_RE = re.compile(CFG["valid_slug_regex"])


def _dir(cfg, key):
    return cfg["dirs"].get(key)


def resolve_path(vault_root, path):
    if os.path.isabs(path):
        return path
    return os.path.join(vault_root, path)


def artifact_dirs(vault_root, cfg=CFG):
    return [os.path.join(vault_root, _dir(cfg, k))
            for k in cfg["artifact_dir_keys"] if _dir(cfg, k)]


def safe_read(abs_path, rel_path=None, vault_root=VAULT, cfg=CFG):
    """Read file; fall back to git show HEAD if iCloud locked (when enabled)."""
    try:
        with open(abs_path, encoding="utf-8") as f:
            return f.read()
    except (OSError, IOError):
        pass
    if rel_path and cfg.get("icloud_git_fallback", True):
        try:
            r = subprocess.run(["git", "show", f"HEAD:{rel_path}"],
                             capture_output=True, text=True, cwd=vault_root, timeout=10)
            if r.returncode == 0:
                return r.stdout
        except Exception:
            pass
    raise IOError(f"Cannot read {abs_path}")


def parse_frontmatter(c):
    m = FRONTMATTER_RE.match(c)
    return m.group(1) if m else None


def has_yaml_field(frontmatter, field):
    return re.search(rf"(?m)^[ \t]*{re.escape(field)}[ \t]*:", frontmatter) is not None


def extract_tags(frontmatter):
    m = INLINE_TAGS_RE.search(frontmatter)
    if m:
        return TAG_TOKEN_RE.findall(m.group(1))
    m = LIST_TAGS_RE.search(frontmatter)
    if not m:
        return []
    tags = []
    for line in m.group(1).splitlines():
        item = re.match(r"^[ \t]+-[ \t]*([^\r\n#]+)", line)
        if item:
            tags.extend(TAG_TOKEN_RE.findall(item.group(1)))
    return tags


def load_schema_tags(vault_root=VAULT, cfg=CFG):
    tags = set()
    schema_rel = cfg["schema_file"]
    c = safe_read(os.path.join(vault_root, schema_rel), schema_rel, vault_root, cfg)
    for line in c.split("\n"):
        m = SCHEMA_TAG_RE.search(line)
        if m:
            tags.add(m.group(1))
    return tags


def check_fm(abs_path, rel_path, vault_root=VAULT, cfg=CFG, required=None):
    if required is None:
        required = cfg["required_frontmatter"]
    try:
        c = safe_read(abs_path, rel_path, vault_root, cfg)
    except Exception as e:
        return {"pass": False, "error": f"cannot_read: {e}"}
    frontmatter = parse_frontmatter(c)
    if not frontmatter:
        return {"pass": False, "error": "no_frontmatter", "auto_fix_type": "frontmatter_missing_field", "missing": required}
    missing = [f for f in required if not has_yaml_field(frontmatter, f)]
    if missing:
        return {"pass": False, "error": "missing_fields", "auto_fix_type": "frontmatter_missing_field", "missing": missing}
    return {"pass": True}


def _raw_tag_strings(frontmatter):
    """Return the raw comma-split tag strings before any tokenization."""
    m = INLINE_TAGS_RE.search(frontmatter)
    if m:
        return [t.strip() for t in re.split(r"[,，]", m.group(1)) if t.strip()]
    m = LIST_TAGS_RE.search(frontmatter)
    if not m:
        return []
    raw = []
    for line in m.group(1).splitlines():
        item = re.match(r"^[ \t]+-[ \t]*([^\r\n#]+)", line)
        if item:
            raw.append(item.group(1).strip())
    return raw


def check_tags(abs_path, rel_path, valid_tags, vault_root=VAULT, cfg=CFG):
    try:
        c = safe_read(abs_path, rel_path, vault_root, cfg)
    except Exception as e:
        return {"pass": False, "error": f"cannot_read: {e}"}
    frontmatter = parse_frontmatter(c)
    if not frontmatter:
        return {"pass": True}
    # Layer 1: raw format — catch bilingual ("x / 中文"), multi-word ("AI agent"),
    # uppercase ("AI"), CJK-only tags before tokenization silently drops them.
    raw_tags = _raw_tag_strings(frontmatter)
    bad_format = [t for t in raw_tags if not VALID_SLUG_RE.match(t)]
    if bad_format:
        return {"pass": False, "error": "invalid_tag_format",
                "auto_fix_type": "tag_not_in_schema",
                "detail": "tags must be lowercase-hyphen slugs (no spaces, slashes, uppercase, or CJK)",
                "invalid_format": bad_format}
    # Layer 2: taxonomy membership
    file_tags = extract_tags(frontmatter)
    invalid = [t for t in file_tags if t not in valid_tags]
    if invalid:
        return {"pass": False, "error": "invalid_tags", "auto_fix_type": "tag_not_in_schema", "invalid": invalid}
    return {"pass": True}


def check_file_has_content(path):
    if not os.path.exists(path):
        return {"pass": False, "error": "not_found"}
    if os.path.getsize(path) <= 0:
        return {"pass": False, "error": "empty_file"}
    return {"pass": True}


def check_wikilinks(abs_path, rel_path, vault_root=VAULT, cfg=CFG):
    try:
        c = safe_read(abs_path, rel_path, vault_root, cfg)
    except Exception as e:
        return {"pass": False, "error": f"cannot_read: {e}"}
    dirs = artifact_dirs(vault_root, cfg)
    broken = []
    for m in re.finditer(r"\[\[([^\]|#]+)", c):
        target = m.group(1)
        if not target.endswith(".md"):
            target += ".md"
        found = any(os.path.exists(os.path.join(d, os.path.basename(target))) for d in dirs)
        if not found:
            broken.append(m.group(1))
    if broken:
        return {"pass": False, "error": "broken_wikilinks", "broken": broken}
    return {"pass": True}


def check_index(vault_root=VAULT, cfg=CFG):
    index_rel = cfg["index_file"]
    try:
        c = safe_read(os.path.join(vault_root, index_rel), index_rel, vault_root, cfg)
    except Exception as e:
        return {"pass": False, "error": f"index_unreadable: {e}"}
    actual = {}
    for name, rel in cfg["dirs"].items():
        p = os.path.join(vault_root, rel)
        actual[name] = len([f for f in os.listdir(p) if f.endswith(".md")]) if os.path.exists(p) else 0
    actual["total"] = sum(actual.values())
    claimed = {}
    for name in actual:
        m = re.search(rf"\^stat-{name}\s+(\d+)", c)
        if m:
            claimed[name] = int(m.group(1))
    # only flag stats the index actually declares — absent anchors are not a mismatch
    mismatches = {k: (claimed[k], actual[k]) for k in claimed if claimed[k] != actual[k]}
    if mismatches:
        return {"pass": False, "error": "index_count_mismatch",
                "auto_fix_type": "index_count_mismatch", "mismatches": mismatches, "actual": actual}
    return {"pass": True}


def check_orphans(vault_root=VAULT, cfg=CFG):
    index_rel = cfg["index_file"]
    try:
        c = safe_read(os.path.join(vault_root, index_rel), index_rel, vault_root, cfg)
    except Exception as e:
        return {"pass": False, "error": f"index_unreadable: {e}"}

    orphans = []
    for key in cfg["orphan_dir_keys"]:
        rel_dir = _dir(cfg, key)
        if not rel_dir:
            continue
        abs_dir = os.path.join(vault_root, rel_dir)
        if not os.path.isdir(abs_dir):
            continue
        for name in sorted(f for f in os.listdir(abs_dir) if f.endswith(".md")):
            stem = os.path.splitext(name)[0]
            # Accept both bare links [[stem]] and path-prefixed links [[dir/stem]],
            # with or without an alias pipe. Path-prefixed forms end in "/stem]]" or "/stem|".
            linked = (f"[[{stem}]]" in c or f"[[{stem}|" in c
                      or f"/{stem}]]" in c or f"/{stem}|" in c
                      or name in c)
            if not linked:
                orphans.append(os.path.join(rel_dir, name))
    if orphans:
        return {"pass": False, "error": "orphan_pages", "orphans": orphans}
    return {"pass": True}


def check_log(source_rel, vault_root=VAULT, cfg=CFG):
    log_rel = cfg["log_file"]
    try:
        c = safe_read(os.path.join(vault_root, log_rel), log_rel, vault_root, cfg)
    except Exception:
        return {"pass": False, "error": "log_unreadable"}
    if source_rel in c:
        return {"pass": True}
    return {"pass": False, "error": "log_entry_missing", "auto_fix_type": "log_entry_missing", "source": source_rel}


def check_stale_content(vault_root=VAULT, cfg=CFG):
    """Flag pages whose `updated` is older than stale_days."""
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=cfg["stale_days"])
    stale = []
    for key in cfg["page_dir_keys"]:
        rel_dir = _dir(cfg, key)
        if not rel_dir:
            continue
        abs_dir = os.path.join(vault_root, rel_dir)
        if not os.path.isdir(abs_dir):
            continue
        for name in sorted(f for f in os.listdir(abs_dir) if f.endswith(".md")):
            path = os.path.join(abs_dir, name)
            try:
                c = safe_read(path, os.path.join(rel_dir, name), vault_root, cfg)
                fm = parse_frontmatter(c)
                if fm:
                    m = re.search(r"(?m)^updated[ \t]*:[ \t]*(\d{4}-\d{2}-\d{2})", fm)
                    if m:
                        updated = datetime.date.fromisoformat(m.group(1))
                        if updated < cutoff:
                            stale.append({"file": os.path.join(rel_dir, name), "updated": m.group(1)})
            except Exception:
                pass
    if stale:
        return {"pass": False, "error": "stale_content", "stale": stale}
    return {"pass": True}


def check_contradictions(vault_root=VAULT, cfg=CFG):
    """Check pages with `contested: true` but missing or empty `contradictions` field."""
    issues = []
    for key in cfg["page_dir_keys"]:
        rel_dir = _dir(cfg, key)
        if not rel_dir:
            continue
        abs_dir = os.path.join(vault_root, rel_dir)
        if not os.path.isdir(abs_dir):
            continue
        for name in sorted(f for f in os.listdir(abs_dir) if f.endswith(".md")):
            path = os.path.join(abs_dir, name)
            try:
                c = safe_read(path, os.path.join(rel_dir, name), vault_root, cfg)
                fm = parse_frontmatter(c)
                if fm and re.search(r"(?m)^contested[ \t]*:[ \t]*true", fm):
                    if not re.search(r"(?m)^contradictions[ \t]*:[ \t]*\[.+\]", fm):
                        issues.append({"file": os.path.join(rel_dir, name), "error": "contested_true_but_no_contradictions_field"})
            except Exception:
                pass
    if issues:
        return {"pass": False, "error": "contradiction_issues", "issues": issues}
    return {"pass": True}


def check_dupes(vault_root=VAULT, cfg=CFG):
    seen = {}
    dupes = []
    for key in cfg["artifact_dir_keys"]:
        rel_dir = _dir(cfg, key)
        if not rel_dir:
            continue
        abs_dir = os.path.join(vault_root, rel_dir)
        if not os.path.isdir(abs_dir):
            continue
        for name in sorted(f for f in os.listdir(abs_dir) if f.endswith(".md")):
            path = os.path.join(abs_dir, name)
            if name in seen:
                dupes.append({"file": name, "paths": [seen[name], path]})
            else:
                seen[name] = path
    if dupes:
        return {"pass": False, "error": "duplicate_entries", "duplicates": dupes}
    return {"pass": True}


def run(artifacts, source_rel, vault_root=VAULT, cfg=CFG):
    results = []
    all_pass = True
    source_abs = resolve_path(vault_root, source_rel)
    schema_rel = cfg["schema_file"]
    schema_available = True
    try:
        valid_tags = load_schema_tags(vault_root, cfg)
    except Exception as e:
        valid_tags = set()
        schema_available = False
        results.append({"file": schema_rel, "check": "tags_schema", "pass": False,
                        "error": f"schema_unreadable: {e}"})
        all_pass = False

    if schema_available and not valid_tags:
        results.append({"file": schema_rel, "check": "tags_schema", "pass": False,
                        "error": "tag_check_unavailable"})
        all_pass = False

    r = check_fm(source_abs, source_rel, vault_root, cfg,
                 required=cfg.get("source_required_frontmatter", cfg["required_frontmatter"]))
    results.append({"file": source_rel, "check": "source_frontmatter", **r})
    if not r["pass"]: all_pass = False

    r = check_file_has_content(source_abs)
    results.append({"file": source_rel, "check": "source_has_content", **r})
    if not r["pass"]: all_pass = False

    for art in artifacts:
        art_abs = resolve_path(vault_root, art)
        rel = os.path.relpath(art_abs, vault_root) if art_abs.startswith(vault_root) else None
        if not os.path.exists(art_abs):
            results.append({"file": art, "check": "exists", "pass": False, "error": "not_found"})
            all_pass = False
            continue
        results.append({"file": art, "check": "exists", "pass": True})

        r = check_fm(art_abs, rel, vault_root, cfg)
        results.append({"file": art, "check": "artifact_frontmatter", **r})
        if not r["pass"]: all_pass = False

        r = check_tags(art_abs, rel, valid_tags, vault_root, cfg)
        results.append({"file": art, "check": "tags", **r})
        if not r["pass"]: all_pass = False

        r = check_wikilinks(art_abs, rel, vault_root, cfg)
        results.append({"file": art, "check": "wikilinks", **r})
        if not r["pass"]: all_pass = False

    r = check_index(vault_root, cfg)
    results.append({"file": cfg["index_file"], "check": "index_counts", **r})
    if not r["pass"]: all_pass = False

    r = check_log(source_rel, vault_root, cfg)
    results.append({"file": cfg["log_file"], "check": "log_entry", **r})
    if not r["pass"]: all_pass = False

    r = check_dupes(vault_root, cfg)
    results.append({"file": "various", "check": "duplicates", **r})
    if not r["pass"]: all_pass = False

    r = check_orphans(vault_root, cfg)
    results.append({"file": cfg["index_file"], "check": "orphan_pages", **r})
    if not r["pass"]: all_pass = False

    r = check_stale_content(vault_root, cfg)
    results.append({"file": "various", "check": "stale_content", **r})
    if not r["pass"]: all_pass = False

    r = check_contradictions(vault_root, cfg)
    results.append({"file": "various", "check": "contradictions", **r})
    if not r["pass"]: all_pass = False

    issues = [x for x in results if not x.get("pass")]
    fixable = [x for x in issues if x.get("auto_fix_type")]

    return {"pass": all_pass, "total_checks": len(results), "failed": len(issues),
            "fixable_count": len(fixable),
            "all_fixable": len(fixable) == len(issues) and len(issues) > 0,
            "issues": issues, "fixable": fixable}


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--artifacts", nargs="+", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--vault", default=VAULT)
    p.add_argument("--config", default=CONFIG_PATH,
                   help="path to config.json (default: $WIKI_INGEST_CONFIG or ~/.hermes/ingestion/config.json)")
    args = p.parse_args()
    cfg = load_config(args.config)
    vault = args.vault or cfg["vault_root"]
    result = run(args.artifacts, args.source, vault, cfg)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if result["pass"] else 1)
