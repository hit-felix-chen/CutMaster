# Layer local settings over versioned configuration

**Status: Superseded by the single-file configuration decision below.**

This ADR originally introduced a sparse sibling configuration overlay. Once
CLI, Mashup-Benchmark, workers, and WebUI converged on the same
`CutMasterApplication` composition root, that second non-secret configuration
authority no longer provided a useful boundary. The implemented replacement is
documented in
[ADR 0022](0022-use-one-authoritative-application-configuration.md).
