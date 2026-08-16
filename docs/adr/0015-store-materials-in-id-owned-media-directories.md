# Store Materials in ID-owned media directories

**Status: Implemented.**

Each Material receives an opaque UUIDv4-based Material ID formatted as
`mat_<uuid>`. The Material Library stores it under the Application Data
Root-relative path `media/<video|music>/<material-id>/`, with the managed source
and reusable analysis owned by that directory. The manifest records the
Material ID, type-scoped exact Material Name, SHA-256 fingerprint, and relative
artifact paths.

Material Name remains the public CLI selector and user-visible label, while
frontend API commands and canonical persistence relations use Material ID.
Neither Material Name nor fingerprint participates in ID or directory
construction. This keeps paths stable when display concepts evolve, avoids
leaking user-provided names into filesystem structure, and prevents
fingerprints from becoming accidental public identifiers.

The manifest schema for this layout is version 2. Earlier schema and directory
formats are not migrated. The existing `.cutmaster/materials-backup/` directory
is deliberately ignored: CutMaster never scans, imports, moves, or modifies it.
An optional subtitle imported with a video is stored as an immutable
Material-owned sidecar. It is covered by the analysis specification and exposed
to Analyser only through the Application-created video runtime handle; callers
do not pass its raw path into the v2 stage request.

Material lifecycle belongs to the Application `materials` service behind a
`MaterialCatalog` port. The local infrastructure implementation owns this
manifest, managed copies, fingerprint verification, and cross-process leases.
The Workflow Analyser consumes an already resolved Material and produces or
reuses Material Memory; catalog lookup, naming, import, and deletion are not
Analyser responsibilities.

The Application maps canonical Material state to a runtime-only
`MaterialRuntimeHandle` while holding its lease. This handle carries the exact
identity, fingerprint, and resolved source/memory paths into Analyser but is
never stored in product records. Analyser independently verifies consistency
and analysis-cache compatibility, then returns result references for the
Application to commit through the catalog.

Planners receives analysed video and music runtime handles that bind Material
Memory to the same exact identities and fingerprints. It does not resolve raw
paths or public names, and the Application retains both Material leases until
planning completes.

Renderer receives the same identities through runtime bindings resolved from a
portable RenderPlan. The Application retains both leases through rendering;
Renderer verifies the plan's expected IDs and fingerprints but never accesses
the Material Catalog itself.
