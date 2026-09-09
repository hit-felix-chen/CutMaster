# 0026 — Use Renderer as MASTER R

Status: Accepted

## Decision

MASTER consists of Material Analyst, Arrangement Architect, Story Editor, Timeline
Scout, Edit Composer, and Renderer. ASTER covers planning plus rendering. The
Planners stage contains four planning agents (A/S/T/E); Application dispatches
Renderer separately after the edit is frozen.

Remove the automatic Revision Editor, its prompt, configuration, and final
revision loop. Edit Composer's selected path is the final planning result before
the existing deterministic compilation and source-window optimization.

## Compatibility

Historical Revision Editor checkpoints and usage records remain readable and
are labelled as legacy, never reclassified as Renderer costs. New planning runs
write no Revision Editor checkpoints. The independent, user-initiated Guided
Revision feature from ADR 0009 remains available.

The planning progress indicator has four milestones. Rendering progress belongs
to the real Renderer Job / Render Variant, not a simulated fifth model stage.

## Experiments

Anchor selection can be disabled explicitly. Edit Composer supports a
first-retained-trajectory mode without pairwise model scoring or beam search.
See [the ablation protocol](../dev/aster-ablations.md) for definitions and reuse
limitations.
