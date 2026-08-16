# Separate Planners compilation from deterministic rendering

**Status: Implemented.**

All decisions that change the edit belong to the Planners stage. This includes
Slot arrangement and candidate selection, output-frame allocation, Beat
adjustment, and source-window optimization. The Planners stage freezes those
decisions in `render_plan.json`.

Renderer is a separate top-level stage. It may encode the selected clips,
prepare selected dialogue vocals, mix audio, and reuse a silent montage cache,
but it must not change source ranges, output ranges, or Story Anchor timing.
Renderer never imports model access or Planners-stage implementations.

This boundary allows one expensive plan to produce multiple BGM-only or
dialogue renders without invoking LLM/VLM services again.

RenderPlan is also separated from runtime media binding. It stores immutable
timeline decisions plus expected Material IDs and fingerprints, not absolute
paths, mtimes, or prepared audio. The Application resolves leased Materials
into a runtime-only binding object for each Renderer invocation, keeping the
same plan portable and reusable across variants and Data Root Migration.

ASTER agents decide the edit, and the deterministic Plan Compiler emits the
RenderPlan. In managed use, the Application commits that plan as the sole
immutable payload of a Frozen Edit; it does not create a second timeline copy.
Direct workflows may render the same contract without creating product history.

Each managed Render Variant also owns an immutable normalized Render
Specification. All of its Attempts use that snapshot rather than current global
configuration; rendering the same Frozen Edit with changed output settings
creates another Variant, preserving Variant identity and reproducibility.
