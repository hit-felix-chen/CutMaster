# Separate Planner compilation from deterministic rendering

All decisions that change the edit belong to Planner. This includes Slot and
candidate selection, output-frame allocation, Beat adjustment, and source-window
optimization. Planner freezes those decisions in `render_plan.json`.

Renderer is a separate top-level stage. It may encode the planned clips,
prepare selected dialogue vocals, mix audio, and reuse a silent montage cache,
but it must not change source ranges, output ranges, or Dialogue Anchor timing.
Renderer never imports model access or planner implementations.

This boundary allows one expensive plan to produce multiple BGM-only or
dialogue renders without invoking LLM/VLM services again.
