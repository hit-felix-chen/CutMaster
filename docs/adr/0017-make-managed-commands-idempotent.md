# Make managed commands idempotent

**Status: SQLite command receipts, asynchronous command responses, FastAPI
`Idempotency-Key`, and SSE correlation implemented.**

Every managed state-changing command carries a client-generated UUID and a
canonical request digest, with a durable SQLite receipt linking it to the first
result. Transport retries return that result across process restarts, while the
same ID with different input is rejected; each new user intent gets a new ID.

The FastAPI adapter transports this value through `Idempotency-Key`.
Asynchronous commands return it with `202 Accepted`, and durable SSE events use
the same correlation identity. This prevents duplicate Materials, ASTER Runs,
Frozen Edits, Render Variants, Attempts, migrations, and other records without
introducing a universal command endpoint.
