# Use one authoritative Application configuration

**Status: Implemented.**

Every CutMaster adapter opens one explicitly selected TOML file through
`CutMasterApplication.open(config_path)`. For the repository setup that file is
`config.toml`; no sibling local TOML overlay is discovered, merged, or written.
An explicitly selected alternative such as `/path/eval.toml` is likewise the
complete authority for that Application instance.

Web Settings atomically updates the selected TOML file after validating the
complete candidate configuration. Updates preserve unedited sections and TOML
comments. The configuration lock lives under the git-ignored
`.cutmaster-control/` directory, so saving settings does not create a second
configuration file. Public provider values are written to the same TOML file;
API keys remain exclusively in the process environment or the sibling `.env`,
which is written with owner-only permissions.

CLI, Benchmark, Web, and managed workers therefore see identical non-secret
configuration whenever they open the same path. Managed Materials, ASTER Runs,
and Render Variants retain their immutable non-secret snapshots, so changes are
prospective and Retry or Resume semantics remain reproducible.
