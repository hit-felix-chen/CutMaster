# Constrain Guided Revision to the ASTER Candidate Space

**Status: Accepted and implemented for Candidate-Bundle-backed Frozen Edits.**

CutMaster's first frontend is not a freeform timeline editor. A user may replace
only a non-anchor Slot with another candidate already validated for that Slot;
Slot timing, chronological order, and Story Anchors remain fixed. This preserves
the narrative and pacing guarantees established by the MASTER team while still
giving users meaningful control, and avoids turning the application into a
second unconstrained editing system whose changes bypass agent reasoning.

Newly completed managed ASTER Runs publish a versioned, integrity-checked
Candidate Bundle beside their RenderPlan. Frozen Edits created before that
publication remain available through a historical read-only Review: CutMaster
does not infer a Candidate Space from the selected clips or expose replacement
controls without the real bundle.

Saving a valid replacement set does not rerun ASTER. The frontend keeps all
staged replacements page-local and sends them atomically; the backend persists
no Revision Draft, and navigation may discard it only through an explicit user
action. A deterministic compiler produces a new RenderPlan, and the Application
commits it as one derived Frozen Edit linked to its source; the previous plan
and Frozen Edit remain unchanged. Any Frozen Edit in the same ASTER Run may be
the source when its Candidate Bundle is available, and one source may have
multiple children, preserving alternate revision branches. The first frontend
shows a creation-ordered version list with parent labels instead of requiring a
graphical version-tree editor.
Candidate, Story Anchor, and chronology validation complete before the history
commit, so an invalid command creates neither a child Frozen Edit nor a partial
plan. Automatic Renderer Preview/Variant creation after a successful revision
remains proposed and is not part of this atomic command.
