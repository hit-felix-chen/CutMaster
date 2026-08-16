# CutMaster

CutMaster turns long-form footage into a finished montage through a three-stage
**CutMaster Workflow**. Its editorial intelligence is organized as the
**MASTER Editing Team** within the first two stages.

## Implementation status

The backend implements the three-stage **Analyser → Planners → Renderer**
workflow, reusable video and music Materials, name-based selection, Material
Fingerprint verification, and single-video/single-music editing. A real
`CutMasterApplication` now exposes seven Application service groups. Its Direct
service owns raw-path and Material-Name resolution, calls all three stages
through handle-only v2 contracts, publishes a versioned Direct Artifact
Manifest, and is the shared execution boundary used by the CLI and
Mashup-Benchmark. The earlier monolithic complete-workflow facade and raw-path
stage DTOs have been removed.

The backend also implements SQLite-backed managed identity and history for
**Edit Projects**, **Material References**, **ASTER Runs**, **Execution
Attempts**, **Frozen Edits**, **Render Variants**, jobs, durable events, command
idempotency, and settings. The local Material Catalog, managed Direct Workflow
Bundles, secret-free Effective Configuration, and Application Data Root
resolution are active code, not design placeholders.

The FastAPI peer adapter, React/Vite Web UI, `cutmaster serve`, and the first
managed Web slice are implemented over the same Application Layer. Projects,
the combined Project Setup, Materials and Memory, ASTER Run submission and
detail polling, Activity, Settings, source-media Range streams, and SPA
packaging are active code. Start editing creates a durable ASTER Run and an
isolated subprocess executes its real Planners work. **Proposed:** Web-managed
Material Analysis, Renderer and Review workers, SSE, and guarded **Data Root
Migration**.

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

**Application Layer** *(Implemented)*:
The framework-independent use-case boundary above the CutMaster Workflow. It
owns Material resolution, product history, execution policy, output ownership,
and settings while exposing the same business operations to CLI, Web, and
Benchmark adapters. It does not contain Analyser, Planners, or Renderer
implementation logic.
_Avoid_: FastAPI, Web backend, complete-workflow facade, CutMaster Workflow

**CutMasterApplication** *(Implemented)*:
The single composition entry point that wires configuration, ports, concrete
infrastructure, and the grouped `direct`, `materials`, `projects`, `runs`,
`renders`, `jobs`, and `settings` Application services. It coordinates object
construction but does not implement those use cases itself.
_Avoid_: monolithic service, complete-workflow facade, Web server

**Peer Adapter** *(CLI, FastAPI Web, and Benchmark implemented)*:
One transport-specific caller of the Application Layer. CLI, FastAPI Web, and
Mashup-Benchmark are peers: none invokes another adapter, and all map their own
inputs and outputs to the same Application use cases.
_Avoid_: CLI wrapper around HTTP, Benchmark wrapper around CLI, Application Layer

**Material Runtime Handle** *(Implemented)*:
A runtime-only binding created by the Application Layer while it holds the
Material lease. It carries the Material ID, expected fingerprint, and resolved
source/Memory locations—and, for video, any verified immutable subtitle
sidecar—needed by a handle-only Workflow stage; it is never persisted,
displayed, or accepted as a public selector.
_Avoid_: Material ID, Material Reference, source path DTO, Managed Artifact Reference

**Render Runtime Bindings** *(Implemented)*:
The runtime-only video and music Material handles supplied separately from a
portable RenderPlan. They let Renderer access verified local media without
placing absolute paths or mutable storage state in the plan.
_Avoid_: RenderPlan, persisted source paths, Render Specification

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
analysis and editing. Material Names are unique within each Material Type;
Materials may be added or deleted, but never replaced.
_Avoid_: File browser, cache directory, project assets

**Material Type**:
The namespace that classifies a Material as `video` or `music`. Material Name
uniqueness and lookup are scoped to one Material Type, so a video and a music
track may share the same name without identifying the same Material.
_Avoid_: file extension, content type, Project Material Set

**Material**:
One immutable source video or music asset in the **Material Library**. It owns
one internal **Material ID**, is publicly selected within its Material Type by a
unique **Material Name**, records a **Material Fingerprint**, and may be
described by Material Memory.
_Avoid_: File path, Material Memory, Material Name

**Material ID**:
The opaque, immutable internal identity of one Material, formatted as
`mat_<uuid>`. It owns that Material's managed directory and canonical database
relations but is not displayed or accepted as a CLI selector. It is independent
of the Material Name and Fingerprint.
_Avoid_: Material Name, Material Fingerprint, managed path, public selector

**Material Name**:
The stable, user-visible identity assigned when a Material is added and unique
within its Material Type. The pair `(Material Type, exact Material Name)` is the
stable selector used by CLI and core lookup callers. The frontend displays the
Name but uses Material ID in API commands, URLs, and canonical relations. A
proposed name that is already in use causes a Name Collision and is never
changed or overwritten automatically.
_Avoid_: Material ID, Material Fingerprint, filename hash, mutable display label,
source path, managed directory

**Candidate Material Name**:
The normalized proposed name used while adding a Material. The CLI defaults it
to the source filename stem and allows it to be set explicitly. If it is
available within the Material Type, that exact normalized value becomes the
final **Material Name**.
_Avoid_: Material Name after allocation, Material Fingerprint, source filename

**Material Name Collision**:
An attempted addition whose Candidate Material Name already identifies a
Material of the same Material Type. It must be resolved by using the existing
Material, choosing another name, or cancelling the addition.
_Avoid_: Automatic suffix, Material replacement, duplicate fingerprint

**Material Fingerprint**:
The internal SHA-256 of a Material's source bytes, recorded when it is added to
protect the immutable source's consistency. It belongs to that Material record
and its immutable managed source but is not a uniqueness constraint, duplicate
detector, path component, or user-facing selector.
_Avoid_: Material ID, Material Name, deduplication key, managed path,
user-visible identifier

**Inconsistent Material**:
A Material whose current source bytes no longer match its recorded
**Material Fingerprint**. It cannot be analysed, planned, or rendered and may
only be deleted and added again as a new Material.
_Avoid_: Updated Material, stale cache, replacement

**Material Analysis**:
The work performed by the **Material Analyst** to produce or reuse Material
Memory for one Material. Retrying unchanged inputs creates another Execution
Attempt for the same Material Analysis rather than another Material.
_Avoid_: Analyser, Material Memory, ASTER Run

**Material Reference** *(Implemented in managed backend state)*:
A dependency held by an **Edit Project** or **ASTER Run** on one exact Material
ID.
A referenced Material cannot be deleted until the project selection is removed
and every referencing ASTER Run is explicitly deleted.
_Avoid_: Source path, copied asset, soft reference

**Edit Project** *(Implemented in the Application and SQLite layers)*:
A creative workspace that groups its project materials, **ASTER Runs**,
**Frozen Edits**, and rendered variants around one editing goal.
_Avoid_: Workflow Run, Material Memory, output directory

**Project Setup** *(Implemented Web UI)*:
The single saved Project page that combines the current video and music
Material selections with the **Creative Brief**. It is the first of the three
Project tabs, followed by Runs and Outputs; Overview, Project Materials, and
Creative Brief are not separate tabs.
_Avoid_: Overview dashboard, setup wizard, autosaved draft

**Project Name**:
The mutable, non-unique display label of an **Edit Project**. It helps people
recognize a workspace but is not the project's stable identity.
_Avoid_: Material Name, project ID, filesystem directory

**Application Data Root** *(Resolution and storage implemented; migration proposed)*:
The single local directory containing CutMaster's frontend database, Material
Library, project artifacts, Direct Workflow Bundles, durable job state, and
logs. It defaults to `CutMaster/.cutmaster/` and may be set to a custom local
directory; individual data categories do not use independently configured
roots. Repository-level bootstrap configuration, secrets, and the root-location
pointer are outside it.
_Avoid_: browser storage, output directory, Material Library

**Managed Artifact Reference** *(Implemented)*:
A portable reference from a frontend-owned record to managed filesystem data.
It uses a stable owning-entity identity (a Material ID for Material artifacts)
and an Application Data Root-relative path; an absolute Path is resolved only
at a local service boundary.
_Avoid_: absolute persisted path, exported copy, source path selector

**Direct Workflow Bundle** *(Implemented)*:
A self-contained artifact bundle produced by synchronous direct execution
without an explicit external output directory. It moves with the Application
Data Root but belongs to no Edit Project, ASTER Run, Activity history, or Render
Variant, is never deleted automatically, and remains until explicit bulk
cleanup.
_Avoid_: Managed Artifact Reference, ASTER Run, external output directory

**Artifact Manifest** *(Implemented)*:
The versioned logical index embedded in a successful Direct Workflow
`result.json`. It maps stable dotted artifact keys to normalized POSIX file paths
relative to that Direct Workflow Bundle, allowing CLI and Benchmark consumers
to locate outputs without reconstructing internal directory names. It never
contains an absolute path, parent traversal, directory entry, Material Catalog
path, or Managed Artifact Reference.
_Avoid_: filesystem scan, absolute result fields, Managed Artifact Reference,
RenderPlan

**Data Root Migration** *(Proposed)*:
The guarded operation for changing a non-empty **Application Data Root**. It
requires that no Execution Attempt is active, pauses dispatch of queued work,
copies and verifies all managed state, and atomically switches roots only after
verification by updating the external root-location pointer. Failure leaves the
original root active; queued jobs remain durable and resume after the switch.
During migration the application permits read-only browsing and playback but
blocks every managed-state mutation.
_Avoid_: editing the path in place, partial move, separate Material migration

**Project Material Set** *(Implemented in managed backend state)*:
The type-specific collection of video or music **Material References** owned by
an **Edit Project**. The first release permits one video and one music Material
while preserving the collection boundary for future multi-source editing.
_Avoid_: Material Library, single source path, Frozen Edit

**ASTER Run** *(Managed Web planning execution implemented)*:
An immutable planning record within an **Edit Project** that snapshots its
Material References, Creative Brief, and effective non-secret configuration.
Its Execution Attempts represent each try; successful managed completion
records a RenderPlan reference and produces an initial **Frozen Edit**. The Web
Start editing command dispatches this Planners work to an isolated local
subprocess with durable Attempt/job state, heartbeats, and structured A/S/T/E/R
agent milestones. Runs, Run detail, and Activity derive their live display state
from the same Execution Attempt projection and poll it only while active.
Run-level usage projection remains Proposed. Any input change creates another
ASTER Run rather than changing an existing one.
_Avoid_: Edit Project, overwritten plan, Guided Revision

**Execution Attempt** *(Implemented in SQLite-backed job state)*:
One try to complete a Material Analysis, ASTER Run, or Render Variant without
changing that object's inputs or identity. Retrying creates another Execution
Attempt; changing an ASTER input creates another ASTER Run instead.
_Avoid_: ASTER Run, overwrite, rerun with changed inputs

**Interrupted Attempt**:
An Execution Attempt deliberately stopped by the user at a safe boundary. Its
completed work remains available for Resume and it is not classified as a
failure.
_Avoid_: Failed Attempt, cancelled Run, deleted work

**Failed Attempt**:
An Execution Attempt ended by an internal error or exhausted recovery policy.
It is distinct from an Interrupted Attempt and may be retried without changing
the parent object's inputs.
_Avoid_: Interrupted Attempt, invalid user input, ASTER Run

**Creative Brief**:
The mutable request for one **Edit Project**, consisting of an **Editing
Intent** and **Target Duration**. An **ASTER Run** snapshots the last explicitly
saved Creative Brief when it starts.
_Avoid_: Prompt Type, model configuration, ASTER Run

**Editing Intent**:
The user's natural-language description of what the finished edit should
communicate and emphasize.
_Avoid_: Prompt Type, internal prompt, video summary

**Target Duration**:
The desired running time of the finished edit. It constrains the ASTER Team's
arrangement but does not prescribe individual Slot lengths. It must be positive
and cannot exceed the selected Music Material's complete source duration.
_Avoid_: Target shot length, source duration, render duration measurement

**Material Memory**:
The reusable, edit-independent account of one **Material**. A Material Memory is
either a **Video Material Memory** or a **Music Memory**.
_Avoid_: Project, preprocessing output, request-specific profile

**Video Material Memory**:
The **Material Memory** of one source video, including its Shots, Segments,
dialogue, visual evidence, and story summary. Its dialogue comes from the
optional source subtitle selected before analysis or from ASR when none is
provided.
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

**RenderPlan**:
The portable, immutable, frame-exact Renderer contract compiled from accepted
editorial decisions. It identifies the expected Materials and exact source and
output ranges but contains no runtime media paths.
_Avoid_: Frozen Edit, final video, mutable timeline, runtime media binding

**Frozen Edit** *(Implemented in managed backend state)*:
The immutable product-history identity of one accepted edit version, belonging
to one **ASTER Run** and owning exactly one **RenderPlan**. An ASTER Run has no
canonical current or final Frozen Edit; any version may parent many **Guided
Revision** versions and may have many **Render Variants** without duplicating
the timeline.
_Avoid_: RenderPlan, final video, mutable timeline, render cache

**Guided Revision** *(Frozen Edit lineage persistence implemented; candidate
validation and Web flow proposed)*:
A user-directed replacement of a non-anchor Slot's selected passage with
another member of its existing **Candidate Space**. It starts from any Frozen
Edit in the same ASTER Run, preserves Slot timing, arrangement constraints, and
Story Anchors, and produces a new child Frozen Edit without changing its source.
_Avoid_: Freeform timeline editing, rerunning ASTER coordination, Revision Editor

**Revision Draft** *(Proposed Web UI state)*:
The uncommitted candidate replacements currently staged for one **Guided
Revision**. Saving them creates one new **Frozen Edit**, while explicitly
discarding them creates no history; before either action they are not
recoverable state.
_Avoid_: Frozen Edit, ASTER Run, persisted draft, autosaved edit version

**Render Specification** *(Implemented in managed backend state)*:
The immutable snapshot of normalized audio mode and output-affecting Renderer
settings owned by one **Render Variant**. A Frozen Edit has at most one
non-deleted Render Variant for an identical Render Specification.
_Avoid_: RenderPlan, output path, Execution Attempt, worker controls

**Render Variant** *(Implemented in the Application and SQLite layers)*:
One audiovisual realization of a **Frozen Edit** whose Renderer settings may
differ without changing any editorial decision; multiple Render Variants may
share one Frozen Edit without creating another ASTER Run. Its managed master
remains inside the Application Data Root until that Variant is permanently
deleted, after which another render has a new Render Variant identity.
_Avoid_: ASTER Run, Frozen Edit, revised timeline

**Unavailable Render Variant** *(Implemented in the managed backend)*:
A retained **Render Variant** whose previously completed managed master is
missing or fails integrity validation. It is a product condition rather than a
**Failed Attempt** or Activity item and may be rendered again under the same
Variant identity.
_Avoid_: Failed Attempt, deleted Render Variant, interrupted rendering

**Exported Copy** *(Proposed Web UI concept)*:
A user-downloaded copy of one Render Variant outside the Application Data Root.
It is not project history and CutMaster does not track later moves or deletion
of that copy.
_Avoid_: Render Variant, managed master, new render

**Dialogue Preview** *(Proposed Web product default)*:
The default **Render Variant**, combining background music with the selected
Story Anchors' original dialogue; with no Story Anchors it contains background
music only. Its state is independent of its ASTER Run: failure or permanent
deletion leaves the completed Frozen Edit and Run available without triggering
ASTER or an automatic render.
_Avoid_: Frozen Edit, BGM-only variant, source-audio mix

**Music Profile**:
A Planners-invocation-specific projection of **Music Memory** onto its requested
output duration, used by the **Arrangement Architect** to shape pacing.
Associating that artifact with a managed **ASTER Run** belongs to the proposed
managed-worker wiring.
_Avoid_: Music Memory, source music analysis, reusable material

## Flagged ambiguities

**Editor**:
In this domain, **Story Editor** owns narrative anchoring, **Edit Composer** owns
sequence assembly, and **Revision Editor** owns final review. The unqualified
term “Editor” should not name an agent.

**Revision**:
The **Revision Editor** performs automatic revision during ASTER coordination.
The accepted frontend design lets a person perform a **Guided Revision**
after reviewing a frozen result. Use the qualified term to distinguish them.

**Task**:
Too ambiguous for the product UI. Use **Material Analysis**, **ASTER Run**,
**Execution Attempt**, or **Render Variant** according to the actual object.

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
> **Domain expert:** Not through the current CLI. The proposed Web UI Guided Revision
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
> **Domain expert:** The name already exists, so the addition pauses before the
> file is uploaded. The user may select the existing Material, choose another
> name, or cancel.

> **Developer:** What if the proposed name is the same but the bytes differ?
>
> **Domain expert:** The bytes do not change the rule: a Material Name Collision
> must be resolved by the user, and no numeric suffix is added automatically.

> **Developer:** Are equal fingerprints always the same Material?
>
> **Domain expert:** No. Fingerprints protect managed-source consistency but do
> not deduplicate the library. Equal bytes added under different available names
> become separate Materials; the user is responsible for avoiding that.

> **Developer:** How does a CLI caller choose between Materials?
>
> **Domain expert:** It uses the exact Material Name with `--video-material` or
> `--music-material`. The library resolves that public selector to a Material ID;
> IDs, hashes, and Material Library paths are not CLI selectors.

> **Developer:** Does a fingerprint mismatch trigger automatic re-analysis?
>
> **Domain expert:** No. The Material becomes an Inconsistent Material and is
> unusable until it is deleted and added again.

> **Developer:** Can a Material be deleted while a Frozen Edit uses it?
>
> **Domain expert:** No. The user must remove the current project selection and
> explicitly delete every ASTER Run that references it; deleting a Run also
> removes that Run's Frozen Edits and Render Variants.

> **Developer:** Is an Edit Project permanently limited to one video and song?
>
> **Domain expert:** The current workflow accepts one video and one song. The
> implemented Project Material Sets use collections so a future multi-source
> workflow can expand without changing this domain concept.

> **Developer:** Does changing the prompt update the previous ASTER Run?
>
> **Domain expert:** Direct execution with `--overwrite` replaces artifacts in
> an explicit output directory. Managed project execution creates another
> immutable ASTER Run record and retains the previous result and usage.

> **Developer:** Does retrying a failed ASTER execution create another Run?
>
> **Domain expert:** No. The immutable inputs have not changed, so it creates
> another Execution Attempt under the same Run and may reuse safe checkpoints.

> **Developer:** Do ASTER and the Application independently generate two edit
> timelines, a RenderPlan and a Frozen Edit?
>
> **Domain expert:** No. Planners deterministically compiles ASTER's accepted
> decisions into one RenderPlan; the Application gives that plan a Frozen Edit
> identity and history relationship without copying or changing the timeline.

> **Developer:** Is a Run failed when a person stops it?
>
> **Domain expert:** No. Its current Attempt is Interrupted and may be resumed;
> Failed is reserved for internal errors or exhausted recovery.

> **Developer:** Does a CLI result stored under `.cutmaster/` become an ASTER
> Run in the Web UI?
>
> **Domain expert:** No. With no explicit output directory it is a Direct
> Workflow Bundle: CutMaster migrates the bundle with its Data Root, but it does
> not belong to project history.
