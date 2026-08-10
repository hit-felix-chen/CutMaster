# Snapshot model prices and separate usage scopes

Every recorded LLM or VLM call snapshots the configured uncached-input,
cache-hit-input, and output prices in CNY per million tokens. Usage artifacts
publish two explicit scopes: `current_run` contains only calls created by the
current process, while `cumulative` includes prior calls already stored in the
same task directory. Both scopes are grouped by Prompt Task and model.

Historical calls are priced from their own snapshots instead of the current
configuration. Legacy calls without a price snapshot remain visible as
unpriced usage and contribute zero to cost, rather than being retroactively
priced with unverifiable rates. Reasoning tokens are reported separately but
are not charged in addition to completion tokens.

Stage artifacts retain detailed calls, and the workflow root stores an
aggregate `model_usage.json`. Benchmark integrations are not responsible for
collecting or publishing these statistics.
