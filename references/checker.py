#!/usr/bin/env python3
"""Deterministic Checker for Wiki Loop Ingest.
No LLM — pure filesystem + text parsing. Handles iCloud locking via git fallback.
"""

import json, os, sys, subprocess, re

VAULT = "/Users/zhuyunjiang/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian Vault"
FRONTMATTER_RE = re.compile(r"^\ufeff?(?:[ \t]*\r?\n)*---\r?\n(.*?)\r?\n---(?:\r?\n|$)", re.DOTALL)
INLINE_TAGS_RE = re.compile(r"(?m)^[ \t]*tags[ \t]*:[ \t]*\[(.*?)\][ \t]*$")
LIST_TAGS_RE = re.compile(
    r"(?ms)^[ \t]*tags[ \t]*:[ \t]*(?:\r?\n)((?:[ \t]+-[ \t]*[^\r\n]+(?:\r?\n|$))+)"
)
TAG_TOKEN_RE = re.compile(r"([a-z][a-z0-9-]*)")
SCHEMA_TAG_RE = re.compile(r"`([a-z][a-z0-9-]*)\s*/")


def resolve_path(vault_root, path):
    if os.path.isabs(path):
        return path
    return os.path.join(vault_root, path)


def artifact_dirs(vault_root):
    return [os.path.join(vault_root, d) for d in ["wiki/sources", "wiki/concepts", "wiki/entities"]]


def safe_read(abs_path, rel_path=None, vault_root=VAULT):
    """Read file; fall back to git show HEAD if iCloud locked."""
    try:
        with open(abs_path, encoding="utf-8") as f:
            return f.read()
    except (OSError, IOError):
        pass
    if rel_path:
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


def load_schema_tags(vault_root=VAULT):
    tags = set()
    c = safe_read(os.path.join(vault_root, "wiki/SCHEMA.md"), "wiki/SCHEMA.md", vault_root)
    for line in c.split("\n"):
        m = SCHEMA_TAG_RE.search(line)
        if m:
            tags.add(m.group(1))
    return tags


def check_fm(abs_path, rel_path, vault_root=VAULT):
    required = ["title", "source", "tags"]
    try:
        c = safe_read(abs_path, rel_path, vault_root)
    except Exception as e:
        return {"pass": False, "error": f"cannot_read: {e}"}
    frontmatter = parse_frontmatter(c)
    if not frontmatter:
        return {"pass": False, "error": "no_frontmatter", "auto_fix_type": "frontmatter_missing_field", "missing": required}
    missing = [f for f in required if not has_yaml_field(frontmatter, f)]
    if missing:
        return {"pass": False, "error": "missing_fields", "auto_fix_type": "frontmatter_missing_field", "missing": missing}
    return {"pass": True}


def check_tags(abs_path, rel_path, valid_tags, vault_root=VAULT):
    try:
        c = safe_read(abs_path, rel_path, vault_root)
    except Exception as e:
        return {"pass": False, "error": f"cannot_read: {e}"}
    frontmatter = parse_frontmatter(c)
    if not frontmatter:
        return {"pass": True}
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


def check_wikilinks(abs_path, rel_path, vault_root=VAULT):
    try:
        c = safe_read(abs_path, rel_path, vault_root)
    except Exception as e:
        return {"pass": False, "error": f"cannot_read: {e}"}
    broken = []
    for m in re.finditer(r"\[\[([^\]|#]+)", c):
        target = m.group(1)
        if not target.endswith(".md"):
            target += ".md"
        found = any(os.path.exists(os.path.join(d, os.path.basename(target))) for d in artifact_dirs(vault_root))
        if not found:
            broken.append(m.group(1))
    if broken:
        return {"pass": False, "error": "broken_wikilinks", "broken": broken}
    return {"pass": True}


def check_index(vault_root=VAULT):
    try:
        c = safe_read(os.path.join(vault_root, "wiki/index.md"), "wiki/index.md", vault_root)
    except Exception as e:
        return {"pass": False, "error": f"index_unreadable: {e}"}
    actual = {}
    for name, d in [("entities", "wiki/entities"), ("concepts", "wiki/concepts"),
                    ("sources", "wiki/sources"), ("comparisons", "wiki/comparative-studies"),
                    ("queries", "wiki/queries")]:
        p = os.path.join(vault_root, d)
        actual[name] = len([f for f in os.listdir(p) if f.endswith(".md")]) if os.path.exists(p) else 0
    actual["total"] = sum(actual.values())
    claimed = {}
    for name in actual:
        m = re.search(rf"\^stat-{name}\s+(\d+)", c)
        if m:
            claimed[name] = int(m.group(1))
    mismatches = {k: (claimed.get(k, 0), actual[k]) for k in actual if claimed.get(k, 0) != actual[k]}
    if mismatches:
        return {"pass": False, "error": "index_count_mismatch",
                "auto_fix_type": "index_count_mismatch", "mismatches": mismatches, "actual": actual}
    return {"pass": True}


def check_orphans(vault_root=VAULT):
    try:
        c = safe_read(os.path.join(vault_root, "wiki/index.md"), "wiki/index.md", vault_root)
    except Exception as e:
        return {"pass": False, "error": f"index_unreadable: {e}"}

    orphans = []
    for rel_dir in ["wiki/concepts", "wiki/entities"]:
        abs_dir = os.path.join(vault_root, rel_dir)
        if not os.path.isdir(abs_dir):
            continue
        for name in sorted(f for f in os.listdir(abs_dir) if f.endswith(".md")):
            stem = os.path.splitext(name)[0]
            if f"[[{stem}]]" not in c and f"[[{stem}|" not in c and name not in c:
                orphans.append(os.path.join(rel_dir, name))
    if orphans:
        return {"pass": False, "error": "orphan_pages", "orphans": orphans}
    return {"pass": True}


def check_log(source_rel, vault_root=VAULT):
    try:
        c = safe_read(os.path.join(vault_root, "wiki/log.md"), "wiki/log.md", vault_root)
    except Exception:
        return {"pass": False, "error": "log_unreadable"}
    if source_rel in c:
        return {"pass": True}
    return {"pass": False, "error": "log_entry_missing", "auto_fix_type": "log_entry_missing", "source": source_rel}


def check_dupes(vault_root=VAULT):
    seen = {}
    dupes = []
    for rel_dir in ["wiki/sources", "wiki/concepts", "wiki/entities"]:
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


def run(artifacts, source_rel, vault_root=VAULT):
    results = []
    all_pass = True
    source_abs = resolve_path(vault_root, source_rel)
    schema_available = True
    try:
        valid_tags = load_schema_tags(vault_root)
    except Exception as e:
        valid_tags = set()
        schema_available = False
        results.append({"file": "wiki/SCHEMA.md", "check": "tags_schema", "pass": False,
                        "error": f"schema_unreadable: {e}"})
        all_pass = False

    if schema_available and not valid_tags:
        results.append({"file": "wiki/SCHEMA.md", "check": "tags_schema", "pass": False,
                        "error": "tag_check_unavailable"})
        all_pass = False

    r = check_fm(source_abs, source_rel, vault_root)
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

        r = check_fm(art_abs, rel, vault_root)
        results.append({"file": art, "check": "artifact_frontmatter", **r})
        if not r["pass"]: all_pass = False

        r = check_tags(art_abs, rel, valid_tags, vault_root)
        results.append({"file": art, "check": "tags", **r})
        if not r["pass"]: all_pass = False

        r = check_wikilinks(art_abs, rel, vault_root)
        results.append({"file": art, "check": "wikilinks", **r})
        if not r["pass"]: all_pass = False

    r = check_index(vault_root)
    results.append({"file": "wiki/index.md", "check": "index_counts", **r})
    if not r["pass"]: all_pass = False

    r = check_log(source_rel, vault_root)
    results.append({"file": "wiki/log.md", "check": "log_entry", **r})
    if not r["pass"]: all_pass = False

    r = check_dupes(vault_root)
    results.append({"file": "various", "check": "duplicates", **r})
    if not r["pass"]: all_pass = False

    r = check_orphans(vault_root)
    results.append({"file": "wiki/index.md", "check": "orphan_pages", **r})
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
    args = p.parse_args()
    result = run(args.artifacts, args.source, args.vault)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if result["pass"] else 1)
