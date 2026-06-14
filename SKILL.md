---
name: wiki-loop-ingest
description: Loop Engineering for Wiki ingestion — batch process raw sources with independent Checker, journal-based state, and human gate. Triggers on "消化队列" or "ingest batch".
version: 1.0.0
---

# Wiki Loop Ingest — Loop Engineering for jimu's Wiki

Replaces the manual "消化 → 继续 → 继续" cycle with a semi-autonomous loop. State lives locally (not on iCloud), Maker and Checker are separated, auto-fix is restricted to whitelisted mechanical errors only.

## Philosophy (from Loop Engineering)

- **Schedule**: reads journal, picks next source
- **Maker**: runs existing INGEST (same as `obsidian-ingest-raw`)
- **Checker**: independent, deterministic verification — NOT the same model as Maker
- **State**: append-only JSONL journal at `~/.hermes/ingestion/run_journal.jsonl`
- **Exit**: immediate stop on failure. No max_rounds. No infinite retry.
- **Human gate**: semantic errors always escalate to user. Only mechanical errors get one fix attempt.

## When to use

- User says "消化队列" / "ingest batch" / "消化 raw 里所有未处理的"
- User drops multiple PDFs into raw/papers/ and wants them all processed
- User wants to eliminate the "继续" interaction for batch work

## When NOT to use

- Single article: use `obsidian-ingest-raw` directly (faster, no overhead)
- Articles requiring heavy human judgment: the loop will stop and ask you anyway

## Pre-requisites

- `~/.hermes/ingestion/config.json` exists (created on first run)
- `~/.hermes/ingestion/run_journal.jsonl` exists (append-only log)
- Vault path matches config.vault_root
- `wiki/SCHEMA.md`, `wiki/index.md`, `wiki/log.md` exist

## Steps

### Phase 0: Prepare batch

1. Read `~/.hermes/ingestion/config.json` for vault_root and raw_dirs
2. Scan `raw_dirs` for `.md` files
3. Filter: exclude files already logged in `wiki/log.md` (check by source path)
4. Filter: exclude files already marked `done` in `run_journal.jsonl` (check by source path + hash)
5. Build batch list. If empty, report "nothing to ingest" and exit.

### Phase 1: Loop — for each file in batch

```
┌─ PER-FILE LOOP ──────────────────────────────────┐
│                                                    │
│  1. PRE-FLIGHT                                     │
│     - Verify source file exists                    │
│     - Compute sha256 of source                     │
│     - Check journal: same hash + done? → skip      │
│     - Verify target directories writable           │
│     - Read SCHEMA.md for current tag taxonomy      │
│     - Journal: {"stage": "started", "source": ..., │
│                  "source_hash": ...}                │
│                                                    │
│  2. MAKER (run existing INGEST)                    │
│     - Load obsidian-ingest-raw skill steps         │
│     - Read SCHEMA.md, index.md, log.md             │
│     - Create wiki/sources/<name>.md                │
│     - Create wiki/entities/<author>.md if new      │
│     - Create wiki/concepts/<idea>.md if new        │
│     - Update index.md via block anchor patches     │
│     - Update log.md                                │
│     - Wait 3-5 seconds for iCloud sync             │
│     - Journal: {"stage": "maker_done",             │
│                  "artifacts": [...]}                │
│                                                    │
│  3. CHECKER (independent verification)             │
│     - Run deterministic checks (see Checker spec)  │
│     - Journal: {"stage": "checker_done",           │
│                  "issues": [...]}                   │
│                                                    │
│  4. IF CHECKER FAILS:                              │
│     - If issues are ALL in auto_fix_whitelist:     │
│       → fix once → re-check                        │
│       → Journal: {"stage": "fix_attempted"}         │
│       → If re-check passes → done                  │
│       → If re-check fails → needs_human, STOP      │
│     - If ANY issue is NOT in whitelist:            │
│       → STOP immediately                           │
│       → Journal: {"stage": "needs_human"}           │
│       → Report to user with full issue list        │
│                                                    │
│  5. IF CHECKER PASSES:                             │
│     - Journal: {"stage": "done"}                   │
│     - Git add + commit (message: "ingest: <name>") │
│     - Git push                                     │
│     - Proceed to next file                         │
│                                                    │
└────────────────────────────────────────────────────┘
```

### Phase 2: Report

After loop ends (all done or stopped on failure), report:

```
消化队列报告
✅ 3 done
⚠️ 1 needs_human: raw/articles/xxx.md — 命名冲突：concept "风格" 已存在
⏭️ 2 skipped (already done)
```

## Checker Specification

The Checker runs deterministic file-level checks. It should NOT be the same LLM invocation as the Maker. Use a separate model call or pure shell/Python.

### Checks (all mechanical, no semantic):

| # | Check | Method | Auto-fix? |
|---|-------|--------|-----------|
| 1 | source file has valid frontmatter (`title`, `source`, `tags`) | parse YAML frontmatter block with BOM/CRLF/leading blank support | ✅ fix_once |
| 2 | tag taxonomy is available from `wiki/SCHEMA.md` | parse schema tags; unreadable/empty schema fails closed | ❌ needs_human |
| 3 | source file has content | `os.path.getsize() > 0` | ❌ needs_human |
| 4 | each declared artifact path exists | filesystem existence check | ❌ needs_human |
| 5 | each artifact has valid frontmatter (`title`, `source`, `tags`) | parse YAML frontmatter block | ✅ fix_once |
| 6 | all artifact tags appear in `SCHEMA.md` taxonomy | parse inline or YAML-list `tags` and validate membership | ✅ fix_once (map to closest) |
| 7 | artifact wikilinks resolve | check `[[...]]` targets exist in `wiki/sources`, `wiki/concepts`, `wiki/entities` | ❌ needs_human |
| 8 | `index.md` stat counters match actual file counts | count files vs stat lines | ✅ fix_once |
| 9 | `log.md` has a new entry for this source | grep source path in log | ✅ fix_once |
| 10 | No duplicate filenames across `wiki/sources`, `wiki/concepts`, `wiki/entities` | full-library filename uniqueness scan | ❌ needs_human |
| 11 | No orphan pages in `wiki/concepts` or `wiki/entities` | cross-reference filenames against `index.md` content | ❌ needs_human |

### Auto-fix whitelist (from config):

- `frontmatter_missing_field`: add missing required fields to frontmatter
- `tag_not_in_schema`: replace with closest valid tag from SCHEMA.md
- `index_count_mismatch`: recount files and update stat lines
- `log_entry_missing`: append standard log entry

All other failures → `needs_human`, STOP.

## iCloud File Locking

Same workaround as `obsidian-ingest-raw`:
- Read locked files: `git show HEAD:<path>`
- Write locked files: edit in /tmp, then `git hash-object -w /tmp/x && git update-index --add --cacheinfo 100644 <hash> <path> && git checkout -f -- <path>`
- After Maker writes, wait 3-5 seconds before Checker reads (avoid sync-delay false positives)

## Idempotency

Before processing a source:
1. Compute `sha256sum <source_file>`
2. Search `run_journal.jsonl` for same `source_path` + `source_hash` + `stage: done`
3. If found → skip (already successfully ingested this exact version)
4. If same `source_path` but different `hash` → source was updated, re-ingest

## Pitfalls

1. **Checker must not be the same model invocation as Maker.** Use a separate terminal() call or delegate_task. This is the core of Loop Engineering — independent verification.
2. **Never retry more than once.** The `fix_once` pattern is deliberate. If one fix doesn't work, the problem is likely semantic, not mechanical.
3. **Stop immediately on `needs_human`.** Don't continue to next file. The user needs to know what failed and why before the batch proceeds.
4. **Journal is append-only.** Never rewrite or truncate. Each entry is one JSON line. Use `>>` not `>`.
5. **Git commit after each successful file.** Don't batch commits — if the system crashes, you want per-file granularity.
6. **iCloud sync delay:** Always wait 3-5 seconds between Maker write and Checker read.
7. **The journal IS the source of truth for what was ingested.** Not log.md. Not index.md. The journal with hashes.

## Verification

After loop completes:
- `wc -l ~/.hermes/ingestion/run_journal.jsonl` >= number of files processed
- Each `stage: done` entry has corresponding `source_hash`
- `git log --oneline` shows one commit per ingested file
- `wiki/log.md` has entries for all done files
- `wiki/index.md` stat counters are accurate

## References

- `references/checker.py`: Deterministic Checker implementation (Python, no LLM)
- `references/journal_schema.json`: Journal entry JSON schema
