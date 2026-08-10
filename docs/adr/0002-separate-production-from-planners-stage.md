# Separate Planners compilation from deterministic rendering

All decisions that change the edit belong to the Planners stage. This includes
Slot arrangement and candidate selection, output-frame allocation, Beat
adjustment, and source-window optimization. The Planners stage freezes those
decisions in `render_plan.json`.

Renderer is a separate top-level stage. It may encode the selected clips,
prepare selected dialogue vocals, mix audio, and reuse a silent montage cache,
but it must not change source ranges, output ranges, or Dialogue Anchor timing.
Renderer never imports model access or Planners-stage implementations.

This boundary allows one expensive plan to produce multiple BGM-only or
dialogue renders without invoking LLM/VLM services again.
