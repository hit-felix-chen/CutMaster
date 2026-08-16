# Publish a versioned Direct Artifact Manifest

**Status: Implemented by Direct execution and consumed by Mashup-Benchmark.**

Every successful complete Direct Workflow writes `result.json` last and adds
two top-level fields without removing or renaming the existing compatibility
fields:

```json
{
  "artifact_manifest_version": "1.0",
  "artifacts": {
    "analyser.video_result": "analyser/analysis_result.json",
    "planners.render_plan": "planners/render_plan.json",
    "renderer.output_video": "renderer/output.mp4"
  }
}
```

The Artifact Manifest version is independent of the `WorkflowResult` schema
version and the RenderPlan schema version. Consumers reject an unsupported
major version, tolerate a later minor version and unknown logical keys, and do
not infer a physical path when a key is absent.

Logical keys are stable public identifiers. Version 1 uses lowercase dotted
names whose segments contain letters, digits, and underscores. Renaming or
removing a key, changing its meaning, or changing the path-resolution rules
requires a new manifest major version. Adding an optional key is a compatible
minor extension. A physical file may move while its logical key remains stable;
that indirection is the reason the manifest exists.

Every value is one normalized POSIX path relative to the Direct Workflow Bundle
root—the directory containing `result.json`. Values are files, not directories.
They must not be empty or absolute, contain a backslash, `.` or `..` segment, or
resolve through a symlink outside the canonical bundle root. Material Catalog
paths, Managed Artifact References, external source paths, and other files
outside the bundle are never placed in this mapping.

The required v1 keys for a successful complete workflow are:

| Logical key | Current compatibility path |
|---|---|
| `workflow.result` | `result.json` |
| `workflow.model_usage` | `model_usage.json` |
| `workflow.log` | `cutmaster.log` |
| `analyser.video_result` | `analyser/analysis_result.json` |
| `analyser.music_result` | `analyser/music/music_analysis_result.json` |
| `planners.result` | `planners/planners_result.json` |
| `planners.render_plan` | `planners/render_plan.json` |
| `renderer.result` | `renderer/render_result.json` |
| `renderer.output_video` | `renderer/output.mp4` |

Version 1 also reserves the following standard optional keys. A producer adds
one only when that file was emitted and validated by the current execution; it
does not publish `null`, an empty string, a directory, or a stale file left by
an earlier overwritten run.

| Logical key | Current compatibility path |
|---|---|
| `analyser.source_subtitle` | `analyser/source.srt` |
| `analyser.dialogue_subtitle` | `analyser/dialogue_merged.srt` |
| `analyser.dialogues` | `analyser/dialogues.json` |
| `analyser.music_memory` | `analyser/music/music_memory.json` |
| `planners.music_profile` | `planners/music_profile.json` |
| `planners.edit_plan` | `planners/edit_plan.json` |
| `planners.dialogue_anchors` | `planners/dialogue_anchors.json` |
| `planners.candidate_pool` | `planners/candidate_pool.json` |
| `planners.raw_script` | `planners/script_raw.json` |
| `planners.selection_diagnostics` | `planners/diagnostics/selection_diagnostics.json` |
| `planners.history` | `planners/diagnostics/planners_history.json` |
| `planners.calls` | `planners/diagnostics/planners_calls.json` |
| `planners.model_usage` | `planners/diagnostics/model_usage.json` |

`application/direct/artifacts.py` owns the key registry, path normalization,
required-key validation, and safe resolution. The producer validates every
non-self reference after all stages complete, writes `result.json` to a sibling
temporary file, and atomically renames it into place. The
`workflow.result` self-reference names that final target. A successful
`WorkflowResult` is not published if a required artifact is absent or escapes
the bundle root.

Mashup-Benchmark reads `artifact_manifest_version`, looks up supported logical
keys, safely resolves their returned relative paths against the task output
root, and never reconstructs paths such as `planners/script_raw.json`. Existing
top-level `WorkflowResult` path fields and the current three-stage output layout
remain during the compatibility period, but new integrations use the manifest.
