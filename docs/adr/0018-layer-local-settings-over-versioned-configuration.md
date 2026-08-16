# Layer local settings over versioned configuration

**Status: implemented, including Effective Configuration, provider Web forms,
atomic Settings overlay, sibling `.env` credential writes, and guarded Data
Root Migration.**

CutMaster keeps `config.toml` as the version-controlled base for defaults and
research controls, while Settings writes editable non-secret values to a sparse,
git-ignored sibling `config.local.toml` overlay. API keys are read from process
environment variables or the git-ignored `.env`; writing `.env` credentials is
exposed only through the local Web Settings flow. The service never returns a
key, preserves process-environment precedence, and reports only configured
state, source, writability, and a short suffix. CLI and Benchmark load the same validated Effective
Configuration through `CutMasterApplication`; local overlay writes are atomic,
and managed Runs and Render Variants snapshot their effective non-secret inputs
for reproducibility.

The overlay is always the selected base file's sibling with `.local` inserted
before `.toml`: `config.toml` uses `config.local.toml`, while `eval.toml` uses
`eval.local.toml`. A missing sibling means base-only configuration, and CutMaster
never falls back to the repository overlay for another explicitly selected base,
preventing Web preferences from contaminating CLI or Benchmark experiments.

Locale and Colour Mode are browser-local presentation preferences. They are
excluded from Effective Configuration, the local TOML overlay, SQLite product
history, and operation snapshots.

The Application loader rejects inline `api_key` values, implicit scalar type
coercion, and non-finite numeric values. It validates the merged document and
then snapshots a canonical, fully defaulted, secret-free value. Runtime
configuration materialization resolves provider secrets only after the
Application selects their environment references. Material storage is not a
second configurable root in the canonical snapshot; it is always derived from
the selected Application Data Root. A historical Material Library path may remain
in the versioned base for low-level loading but is rejected in the local Application
overlay.

Settings changes apply prospectively. Material Analysis, ASTER Run, and Render
Variant records retain the effective non-secret specification captured at
creation, and their queued, retried, or resumed Attempts continue using it. A
Direct Workflow keeps the Effective Configuration loaded at process start. API
keys are excluded from snapshots. Direct execution resolves them when its
runtime configuration is created; managed Material Analysis, ASTER planning,
and Renderer workers resolve them independently when their Execution Attempt
starts.
