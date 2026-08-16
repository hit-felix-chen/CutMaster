# Constrain Guided Revision to the ASTER Candidate Space

**Status: Accepted and implemented for every valid Frozen Edit.**

CutMaster's first frontend is not a freeform timeline editor. A user may replace
only a non-anchor Slot with another candidate already validated for that Slot;
Slot timing, chronological order, and Story Anchors remain fixed. This preserves
the narrative and pacing guarantees established by the MASTER team while still
giving users meaningful control, and avoids turning the application into a
second unconstrained editing system whose changes bypass agent reasoning.

Every managed Frozen Edit is associated with a versioned, integrity-checked
Candidate Bundle alongside its RenderPlan. Bundle presence, recorded digests,
Candidate Space, and the selected candidate for every required Slot are part of
Frozen Edit validity. Missing, damaged, or incomplete data returns
`review_artifact_unavailable`: CutMaster does not infer a Candidate Space from
selected clips and does not expose a read-only compatibility Review.

Saving a valid replacement set does not rerun ASTER. The frontend keeps all
staged replacements page-local and sends them atomically; the backend persists
no Revision Draft, and navigation may discard it only through an explicit user
action. A deterministic compiler produces a new RenderPlan, and the Application
commits it as one derived Frozen Edit linked to its source; the previous plan
and Frozen Edit remain unchanged. Any valid Frozen Edit in the same ASTER Run
may be the source, and one source may have
multiple children, preserving alternate revision branches. The first frontend
shows a creation-ordered version list with parent labels instead of requiring a
graphical version-tree editor.
Candidate, Story Anchor, and chronology validation complete before the history
commit, so an invalid command creates neither a child Frozen Edit nor a partial
plan. Renderer Variant creation remains a separate command/lifecycle after this
atomic history commit; the implemented default Dialogue Preview and explicit
BGM-only variants never weaken Guided Revision's all-or-nothing boundary.
