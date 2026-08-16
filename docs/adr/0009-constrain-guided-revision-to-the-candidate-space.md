# Constrain Guided Revision to the ASTER Candidate Space

**Status: Proposed Web product flow; Frozen Edit lineage persistence is implemented.**

CutMaster's first frontend is not a freeform timeline editor. A user may replace
only a non-anchor Slot with another candidate already validated for that Slot;
Slot timing, chronological order, and Story Anchors remain fixed. This preserves
the narrative and pacing guarantees established by the MASTER team while still
giving users meaningful control, and avoids turning the application into a
second unconstrained editing system whose changes bypass agent reasoning.

Saving a valid replacement set does not rerun ASTER. The frontend keeps all
staged replacements page-local and sends them atomically; the backend persists
no Revision Draft, and navigation may discard it only through an explicit user
action. A deterministic compiler produces a new RenderPlan, and the Application
commits it as one derived Frozen Edit linked to its source; the previous plan
and Frozen Edit remain unchanged. Any Frozen Edit in the same ASTER Run may be
the source, and one source may have multiple children, preserving alternate
revision branches. The first frontend shows a creation-ordered version list
with parent labels instead of requiring a graphical version-tree editor.
