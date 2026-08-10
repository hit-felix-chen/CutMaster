# CutMaster

CutMaster turns long-form footage into a finished montage through a three-stage
**CutMaster Workflow**. Its editorial intelligence is organized as the
**MASTER Editing Team** within the first two stages.

## Implementation status

The current backend implements the three-stage workflow, reusable video and
music Materials, name-based selection, Material Fingerprint verification, and
single-video/single-music editing. The project-layer concepts **Material
Reference**, **Edit Project**, **Project Material Set**, immutable **ASTER Run**
history, project-level **Frozen Edit**, and **Guided Revision** are proposed
frontend/domain designs and are not implemented yet. Definitions and examples
for those proposed concepts describe the intended contract, not current backend
behaviour.

## Language

**CutMaster Workflow**:
The three-stage process **Analyser → Planners → Renderer**. The stages respectively
build reusable material understanding, decide the edit, and realize the frozen
edit as a video.
_Avoid_: MASTER Editing Team, six-stage agent chain, pipeline

**Analyser**:
The first workflow stage. It uses the **Material Analyst** to produce reusable
**Material Memory** from source video and music assets.
_Avoid_: Material Analyst when naming the stage, preprocessing

**Planners**:
The second workflow stage. It uses the **ASTER Team** to make and freeze
all editorial decisions before rendering.
_Avoid_: ASTER Team when naming the stage, monolithic editor

**Renderer**:
The final, deterministic workflow stage that realizes a frozen edit as one video
variant without making editorial decisions.
_Avoid_: Production, Rendering Agent, editing agent

**MASTER Editing Team**:
CutMaster's six-role editorial intelligence: one **Material Analyst** followed by
the five-agent **ASTER Team**. The **Renderer** is not a member.
_Avoid_: CutMaster Workflow, Renderer, six-stage workflow

**ASTER Team**:
The Planners-stage team formed by the **Arrangement Architect**, **Story Editor**,
**Timeline Scout**, **Edit Composer**, and **Revision Editor**.
_Avoid_: Planners when referring to the team, monolithic editor

**Editorial Agent**:
A role-bounded decision maker that owns one editorial responsibility and may use
model reasoning, deterministic tools, or both.
_Avoid_: LLM call, prompt stage

**Material Analyst**:
The M agent that converts source video and music assets into reusable
**Material Memory** before edit-specific decisions begin.
_Avoid_: Analyser when referring to the agent, preprocessing

**Arrangement Architect**:
The A agent that defines the montage's Slot arrangement, pacing, emotional
progression, and narrative structure.
_Avoid_: Slot Scheduler, Rhythm Scheduler

**Story Editor**:
The S agent that uses selected original dialogue to anchor story events,
character arcs, and user intent.
_Avoid_: Anchor Editor, Dialogue Anchor Selector

**Timeline Scout**:
The T agent that searches the source timeline for multiple validated visual
candidates for each unanchored Slot.
_Avoid_: Candidate Retriever, Visual Retrieval

**Edit Composer**:
The E agent that assembles a globally coherent candidate sequence under temporal
and visual constraints.
_Avoid_: Sequence Selector, Beam Search stage

**Revision Editor**:
The R agent that reviews a composed edit and replaces weak choices without
leaving the established candidate space.
_Avoid_: Script Reviewer, Reviser

**Material Library**:
The collection of immutable source video and music **Materials** available for
analysis and editing. Materials may be added or deleted, but never replaced.
Repeated ingestion may reuse an existing Material only when both its candidate
name family and internal fingerprint match.
_Avoid_: File browser, cache directory, project assets

**Material**:
One immutable source video or music asset in the **Material Library**, identified
by its unique **Material Name**, protected by a **Material Fingerprint**, and
optionally described by Material Memory.
_Avoid_: File path, Material Memory

**Material Name**:
The stable, user-visible identity assigned when a Material is added. It is also
the exact selector used by CLI and service callers. A candidate-name collision
with different content is resolved by appending a numeric suffix such as
`Film (2)` or `Film (3)` to the newly added Material.
_Avoid_: Material Fingerprint, filename hash, mutable display label, source path

**Candidate Material Name**:
The normalized proposed name used while adding a Material. The CLI defaults it
to the source filename stem and allows it to be set explicitly. It defines the
name family used for reuse and numeric-suffix allocation, but the final assigned
**Material Name** is the only later selector.
_Avoid_: Material Name after allocation, Material Fingerprint, source filename

**Material Fingerprint**:
The internal SHA-256 of a Material's source bytes, recorded when it is added to
protect the immutable source's consistency. The library also compares it only
within a matching Candidate Material Name family to recognize repeated
ingestion; it never merges Materials proposed under different names.
_Avoid_: Material Name, global deduplication key, user-visible identifier

**Inconsistent Material**:
A Material whose current source bytes no longer match its recorded
**Material Fingerprint**. It cannot be analysed, planned, or rendered and may
only be deleted and added again as a new Material.
_Avoid_: Updated Material, stale cache, replacement

**Material Reference** *(Proposed; reference persistence is not implemented)*:
A future dependency held by an **Edit Project**, **ASTER Run**, or **Frozen
Edit** on one exact Material. Once the project layer is implemented, a
referenced Material will not be deletable until those dependent edits are
removed. The current low-level Material Library deletion API accepts the
reference-check result from its caller.
_Avoid_: Source path, copied asset, soft reference

**Edit Project** *(Proposed; not implemented)*:
A future creative workspace that groups its project materials, **ASTER Runs**,
**Frozen Edits**, and rendered variants around one editing goal.
_Avoid_: Workflow Run, Material Memory, output directory

**Project Material Set** *(Proposed; not implemented)*:
The future type-specific collection of video or music **Material References**
owned by an **Edit Project**. The current backend accepts exactly one video and
one music Material per workflow request; the proposed collection keeps the
domain extensible when multiple sources are supported.
_Avoid_: Material Library, single source path, Frozen Edit

**ASTER Run** *(Proposed immutable project history; not implemented)*:
A future immutable attempt within an **Edit Project** that freezes its Material
References, creative request, arrangement settings, and model usage and
produces an initial **Frozen Edit**. In the proposed project layer, any input
change creates another ASTER Run. The current backend writes one set of
Planners artifacts to the requested output directory and may overwrite it when
explicitly requested.
_Avoid_: Edit Project, overwritten plan, Guided Revision

**Material Memory**:
The reusable, edit-independent account of one **Material**. A Material Memory is
either a **Video Material Memory** or a **Music Memory**.
_Avoid_: Project, preprocessing output, request-specific profile

**Video Material Memory**:
The **Material Memory** of one source video, including its Shots, Segments,
dialogue, visual evidence, and story summary.
_Avoid_: Video cache, Music Memory

**Music Memory**:
The **Material Memory** of one complete source track, containing intrinsic
musical structure such as tempo, beats, accents, energy, and sections.
_Avoid_: Music Profile, BGM preprocessing, project music

**Slot**:
One arranged interval on the output timeline with an editorial purpose, target
duration, emotional intent, and visual requirements.
_Avoid_: Clip, scene

**Story Anchor**:
A selected passage of original dialogue together with its source-synchronous
picture that fixes a key narrative moment to a Slot.
_Avoid_: Subtitle, voice-over

**Candidate Space**:
The validated set of source-timeline alternatives from which the final visual
choice for each unanchored Slot may be made.
_Avoid_: Search results, retrieved clips

**Frozen Edit** *(Proposed project entity; not implemented)*:
A future complete, versioned set of source choices and output timing that the
**Renderer** can realize without editorial discretion. The current executable
equivalent is `RenderPlan`, but it is not yet managed as project history.
_Avoid_: Final video, mutable timeline, render cache

**Guided Revision** *(Proposed; not implemented)*:
A future user-directed replacement of one Slot's selected passage with another
member of its existing **Candidate Space**. It will preserve Slot timing and
arrangement constraints and produce a new **Frozen Edit**.
_Avoid_: Freeform timeline editing, rerunning ASTER coordination, Revision Editor

**Music Profile**:
A Planners-invocation-specific projection of **Music Memory** onto its requested
output duration, used by the **Arrangement Architect** to shape pacing. It will
belong to an **ASTER Run** once project history is implemented.
_Avoid_: Music Memory, source music analysis, reusable material

## Flagged ambiguities

**Editor**:
In this domain, **Story Editor** owns narrative anchoring, **Edit Composer** owns
sequence assembly, and **Revision Editor** owns final review. The unqualified
term “Editor” should not name an agent.

**Revision**:
The **Revision Editor** performs automatic revision during ASTER coordination.
The proposed project layer will let a person perform a **Guided Revision**
after reviewing a frozen result. Use the qualified term to distinguish them.

## Example dialogue

> **Developer:** Does the Arrangement Architect choose source timestamps?
>
> **Domain expert:** No. It defines the Slots. The Story Editor fixes a few
> Story Anchors, and the Timeline Scout builds the remaining Candidate Space.
>
> **Developer:** Who chooses the final clip for each Slot?
>
> **Domain expert:** The Edit Composer assembles the sequence, then the Revision
> Editor may replace weak choices only within that Candidate Space.

> **Developer:** Is the Renderer the last member of the MASTER Editing Team?
>
> **Domain expert:** No. MASTER makes the editorial decisions across the Analyser
> and Planners stages; the Renderer deterministically realizes the frozen plan.

> **Developer:** Can a person drag any source clip onto the output timeline?
>
> **Domain expert:** Not in the current backend. The proposed Guided Revision
> will replace a Slot only with an existing member of its Candidate Space,
> producing a new Frozen Edit.

> **Developer:** Can two projects using the same song share their Music Profile?
>
> **Domain expert:** They share one Music Memory. Each Planners invocation
> derives its own Music Profile because its requested output duration may
> differ; the future project layer will associate it with an ASTER Run.

> **Developer:** What happens when I upload the same file twice with the same
> candidate name?
>
> **Domain expert:** If its SHA-256 also matches, the existing Material and its
> completed analysis are reused.

> **Developer:** What if the proposed name is the same but the bytes differ?
>
> **Domain expert:** The new Material receives the next public name in that
> family, such as `Film (2)` or `Film (3)`; existing content is never replaced.

> **Developer:** Are equal fingerprints always the same Material?
>
> **Domain expert:** No. Equal fingerprints are reused only inside the same
> candidate-name family. Explicitly adding the same bytes as `Trailer Source`
> and `Archive Source` creates two independently named Materials.

> **Developer:** How does a CLI caller choose between Materials?
>
> **Domain expert:** It uses the exact Material Name with `--video-material` or
> `--music-material`. Hashes and Material Library paths are not selectors.

> **Developer:** Does a fingerprint mismatch trigger automatic re-analysis?
>
> **Domain expert:** No. The Material becomes an Inconsistent Material and is
> unusable until it is deleted and added again.

> **Developer:** Can a Material be deleted while a Frozen Edit uses it?
>
> **Domain expert:** The current low-level deletion API relies on its caller to
> supply the reference check. Once the proposed project layer is implemented,
> its Material Reference will protect it until the dependent edit is removed.

> **Developer:** Is an Edit Project permanently limited to one video and song?
>
> **Domain expert:** The current backend accepts one video and one song. The
> proposed Project Material Sets are collections so the future project contract
> can expand without changing this domain concept.

> **Developer:** Does changing the prompt update the previous ASTER Run?
>
> **Domain expert:** Today, `--overwrite` replaces artifacts in the selected
> output directory. The proposed project layer will instead create another
> immutable ASTER Run and retain the previous result and usage.
