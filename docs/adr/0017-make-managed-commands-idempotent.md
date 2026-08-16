# Make managed commands idempotent

**Status: SQLite command receipts and FastAPI `Idempotency-Key` transport
implemented for the exposed synchronous commands.**

Every managed state-changing command carries a client-generated UUID and a
canonical request digest, with a durable SQLite receipt linking it to the first
result. Transport retries return that result across process restarts, while the
same ID with different input is rejected; each new user intent gets a new ID.

The FastAPI adapter transports this value through `Idempotency-Key`. Proposed
asynchronous commands will return it with `202 Accepted`, and later SSE events
will use it for
correlation. This prevents duplicate ASTER Runs, Frozen Edits, Attempts, and
other records without introducing a universal command endpoint.
