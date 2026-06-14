# Wiki Loop Ingest

[中文版](README_zh.md)

A skill for batch LLM Wiki ingestion that turns the manual "ingest → continue → continue" cycle into a semi-autonomous loop. Built on Loop Engineering principles: a scheduler reads the queue, the Maker runs the existing ingest pipeline to generate Wiki pages, and an independent Checker performs deterministic verification — stop immediately on failure, proceed only on success.

## What it does

- Batch-ingests multiple sources from `raw/` directories
- Reuses the existing Maker pipeline to create `wiki/sources`, `wiki/concepts`, `wiki/entities` pages
- Tracks state in a local append-only journal instead of relying on chat context
- Uses an independent Checker to limit auto-continuation to mechanical errors — semantic issues always escalate to a human

## How it works

Maker-Checker separation is the core:

- **Maker**: runs the standard ingest pipeline — converts a raw source into Wiki pages, updates `wiki/index.md` and `wiki/log.md`
- **Checker**: runs independently of the Maker (separate process / deterministic script), referencing `references/checker.py` — never the same LLM invocation
- **Journal**: writes every processing step to `~/.hermes/ingestion/run_journal.jsonl` — used to skip completed items and detect updated sources
- **Fail fast**: whitelisted mechanical errors get one fix attempt; if the re-check still fails, or any non-whitelisted issue appears, the loop stops immediately and asks for human intervention

This is the Loop Engineering boundary: scheduling, generation, verification, and state are separated — the model never gets to both generate and sign off on its own output.

## Prerequisites

The following local state must exist:

- `~/.hermes/ingestion/config.json`
  - Must contain at least `vault_root`
  - Typically also defines `raw_dirs` to scan
- `~/.hermes/ingestion/run_journal.jsonl`
  - Append-only run log
  - Create with `touch` if it doesn't exist yet
- Wiki directory structure and base files
  - `wiki/SCHEMA.md` — tag taxonomy
  - `wiki/index.md` — navigable index
  - `wiki/log.md` — ingestion log
  - Directories: `wiki/sources`, `wiki/concepts`, `wiki/entities`

## Usage

Say this in a Hermes / agent conversation:

```text
消化队列
```

This triggers the skill to:

1. Read config and journal
2. Scan for unprocessed raw sources
3. Process each file through Maker → Checker
4. On success, write journal and continue; on failure, stop and report immediately

For single articles, use the single-file ingest directly — it's lighter and faster.
