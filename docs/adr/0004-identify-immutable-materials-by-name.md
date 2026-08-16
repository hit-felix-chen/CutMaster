# Identify immutable Materials by type-scoped unique names

**Status: Implemented in the backend; Web collision UI proposed.**

The Material Library has separate video and music namespaces. Within each
Material Type, the stable user-visible Material Name is unique. Before file
transfer, a proposed name collision must be surfaced to the user and resolved
by selecting the existing Material, entering another name, or cancelling.
CutMaster never appends a numeric suffix or overwrites the existing Material
automatically. CLI and core lookup callers select a Material by the pair
`(Material Type, exact Material Name)` rather than by fingerprint or managed
path. Frontend API commands and canonical relations use Material ID while the UI
shows the Material Name.

The low-level `add` operation enforces this strict import rule atomically. The
Application Material service and direct use case expose an idempotent `ensure`
boundary for repeatable CLI and automation: it may return an existing Material
only when type, exact name, and the already-bound fingerprint all match. It
never allocates a new name. An occupied name with different bytes remains a
collision. Analyser receives the resolved Material and only builds or reuses
compatible Material Memory.

After an addition is accepted, the Material record stores its SHA-256
fingerprint as an internal consistency guard. It is not used to compare
different Material records or prevent equal bytes from being added under
different names; avoiding such duplicates is the user's responsibility. A
fingerprint mismatch marks the managed Material as inconsistent and still
requires deleting and adding it again because source replacement is unsupported.
