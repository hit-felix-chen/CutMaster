# Use portable Managed Artifact References

**Status: Implemented, including guarded Data Root Migration.**

Managed backend state stores stable entity identities—including Material ID
for every Material relation—and Application Data Root-relative artifact paths
instead of persisting the root's absolute path.
The application layer resolves absolute Paths only when calling local stage,
media, or filesystem services. This makes a verified Data Root copy usable at
its destination without transactionally rewriting every Material Memory,
Frozen Edit, result, and database row; absolute-path core result files remain
Attempt artifacts rather than canonical project references.

A git-ignored `CutMaster/.cutmaster-location` bootstrap file sits outside the
Data Root and points to a custom root; its absence selects
`CutMaster/.cutmaster/`. The repository-level `.env`, `config.toml`, and
git-ignored `config.local.toml` also stay outside migration. The historical
`.cutmaster/materials-backup/` Material Library and pre-refactor workflow output
directories are left unchanged and are never scanned, imported, or copied by
Data Root Migration.

The pointer must be a regular, non-symlink file containing one absolute path;
filesystem root is never a valid Data Root. Only a genuinely absent pointer
selects the default—an unreadable, malformed, or wrong-kind pointer fails
startup instead of silently opening an empty default library.
The selected path may be absent or an existing directory, but never an existing
file. Later migration and cleanup use cases additionally establish and verify a
dedicated-root ownership marker before destructive storage operations.

Artifact ownership is explicit rather than inferred from path containment. A
managed use case creates a Managed Artifact Reference from an owning entity ID;
an arbitrary CLI path cannot become one. With no explicit CLI output path,
CutMaster allocates a non-project Direct Workflow Bundle under `direct/`; an
explicit path must be outside the Data Root and remains external.

RenderPlan follows the same rule: it persists Material IDs and expected
fingerprints but no absolute media paths or mtimes. The Application resolves
runtime bindings under Material leases for each render. This new portable plan
schema is v2. Refactored rendering rejects v1 absolute-path plans as unsupported
and does not migrate, rewrite, or delete them.

A Direct Artifact Manifest is a different boundary: it indexes files inside one
Direct Workflow Bundle by stable logical key and bundle-root-relative path. It
does not create a Managed Artifact Reference, confer product ownership, or point
into the Material Catalog or managed Project history. Its schema is defined by
[ADR 0020](0020-publish-a-versioned-direct-artifact-manifest.md).
