# Snapshot model prices and separate usage scopes

**Status: Implemented for Workflow artifacts and managed Run projections.**

Every recorded LLM or VLM call snapshots the configured uncached-input,
cache-hit-input, and output prices in CNY per million tokens. Usage artifacts
publish two explicit scopes: `current_run` contains only calls created by the
current process, while `cumulative` includes prior calls already stored in the
same task directory. Both scopes are grouped by Prompt Task and model.

Historical calls are priced from their own snapshots instead of the current
configuration. Earlier calls without a price snapshot remain visible as
unpriced usage and contribute zero to cost, rather than being retroactively
priced with unverifiable rates. Reasoning tokens are reported separately but
are not charged in addition to completion tokens.

Stage artifacts retain detailed calls, and the workflow root stores an
aggregate `model_usage.json`. Benchmark integrations are not responsible for
collecting or publishing these statistics.

Managed ASTER Attempts persist aggregate usage with their immutable Run and
surface it in Run detail. Resumed Attempts distinguish usage created by the
current process from cumulative usage restored from completed stage
checkpoints; raw calls, prompts, provider responses, and credentials are not
stored in those checkpoints or returned by the Web projection.
