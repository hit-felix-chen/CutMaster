# CutMaster

CutMaster turns long-form footage into a finished montage through a three-stage
**CutMaster Workflow**. Its editorial intelligence is organized as the
**MASTER Editing Team** within the first two stages.

## Implementation status

The backend implements the three-stage **Analyser → Planners → Renderer**
workflow, reusable video and music Materials, name-based selection, Material
Fingerprint verification, and single-video/single-music editing. A real
`CutMasterApplication` exposes seven Application service groups. CLI, FastAPI
Web, the isolated Worker, and Mashup-Benchmark are peer inbound adapters over
the same Application Layer. Complete CLI and Benchmark executions enter through
the public `cutmaster.application.workflow` contract and
`CutMasterApplication.workflows`; they create the same Material, Edit Project,
ASTER Run, Execution Attempt, Frozen Edit, and Render Variant history as Web
and accept no custom output directory. The Worker dispatches already-claimed
durable jobs to Application-owned Material, planning, and rendering executors.
No adapter invokes another adapter; the earlier adapter-specific synchronous
execution facades and raw-path stage DTOs have been removed.

The backend also implements SQLite-backed managed identity and history for
**Edit Projects**, **Material References**, **ASTER Runs**, **Execution
Attempts**, **Frozen Edits**, **Render Variants**, jobs, durable events, command
idempotency, and settings. The local Material Catalog, managed workflow
receipts, secret-free Effective Configuration, and Application Data Root
resolution are active code, not design placeholders.

The FastAPI peer adapter, React/Vite Web UI, `cutmaster serve`, and the first
managed Web slice are implemented over the same Application Layer. Projects,
the combined Project Setup, Materials and Memory, Activity, Settings, source-
media Range streams, and SPA packaging are active code. Web Material import
preflights names and files, accepts an optional video subtitle, runs the real
Analyser in a managed subprocess, publishes bounded annotation-free video covers
and bar-style music energy previews, and supports Retry, Resume, Stop, and
guarded deletion. Start editing creates a
durable ASTER Run; real Planners work commits its RenderPlan and Candidate
Bundle, supports Retry, Run again, deletion and persisted usage, and resumes an
Interrupted Attempt only from a validated complete A/S/T/E/R boundary or the
internal replan-pending boundary. It does not resume inside a model or media
operation. Frozen Edit Review, atomic Guided
Revision, strict Render Specifications, real Renderer Attempts, automatic
Dialogue Preview, Render Variant/Outputs lifecycle, and managed downloads are
implemented.

One local Job Supervisor schedules Analyser, Planners, and Renderer work with a
durable FIFO queue, concurrency capacity, per-owner serialization, heartbeat
leases, cooperative cancellation, and orphan recovery, then launches the peer
Worker for each claimed job. One global SSE stream
projects durable events with `Last-Event-ID` replay and `resync_required` REST
recovery. Provider presets/custom connections, first-run Setup, per-capability
connection tests, atomic `config.toml`/`.env` writes, structured Activity
navigation with paginated recent work, and guarded **Data Root Migration** are
also implemented. Material and ASTER Run detail expose each current Attempt's
absolute managed Job Log path plus an on-demand viewer backed by the retained
file. Still deferred are persistent application notifications, the richer
Activity log drawer, generated OpenAPI TypeScript drift checks in CI,
multi-user/cloud operation, and broader provider and media port injection.

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
and settings while exposing the same business operations to CLI, FastAPI Web,
Worker, and Benchmark adapters. It owns the complete-workflow coordinator and
managed job executors, but does not contain Analyser, Planners, or Renderer
implementation logic.
_Avoid_: FastAPI, Web backend, complete-workflow facade, CutMaster Workflow

**CutMasterApplication** *(Implemented)*:
The single composition entry point that wires configuration, ports, concrete
infrastructure, and the grouped `materials`, `projects`, `runs`, `renders`,
`jobs`, `settings`, and `workflows` Application services. Its public complete-
workflow boundary is `CutMasterApplication.workflows`, whose command and receipt
types are exported from `cutmaster.application.workflow`. The composition root
coordinates object construction but does not implement those use cases itself.
_Avoid_: monolithic service, complete-workflow facade, Web server

**Managed Workflow Coordinator** *(Implemented)*:
The Application-owned complete-execution service exposed as
`CutMasterApplication.workflows`. It resolves or creates managed Materials,
creates an Edit Project and ASTER Run, freezes the accepted edit, renders a
Render Variant, and returns a **Managed Workflow Receipt**. Its public command
and receipt types are exported from `cutmaster.application.workflow`, and it has
no knowledge of terminal flags, HTTP payloads, worker arguments, or benchmark
task formats.
_Avoid_: adapter workflow, transport facade, CutMaster Workflow

**Peer Inbound Adapter** *(CLI, FastAPI Web, Worker, and Benchmark implemented)*:
One transport-specific caller of the Application Layer. CLI, FastAPI Web, and
Mashup-Benchmark map their own inputs and outputs to the same managed
Application use cases, while the isolated Worker maps claimed durable jobs to
Application-owned job execution. CLI and Benchmark synchronously enter through
`CutMasterApplication.workflows`; Web submits durable jobs through Application
services, and the Job Supervisor launches the Worker. The four adapters are
peers: none imports or calls another adapter.
_Avoid_: CLI wrapper around HTTP, Benchmark wrapper around CLI, Web-owned Worker,
Application Layer

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
Attempt for the same Material Analysis rather than another Material. A Ready
Material is never analysed again. A Failed Attempt may be retried and an
Interrupted Attempt may be resumed, while at most one Material Analysis Attempt
may be active for a Material at a time. Valid completed checkpoints may be
reused by that recovery Attempt. During Analysis, the immutable source,
lifecycle state, progress, and Job Log remain inspectable, but intermediate
checkpoints and partial Material Memory are never product data. Material Memory
and its derived previews become visible together only after complete successful
publication. Replaying the same command resolves to its original Attempt, while
a distinct duplicate command resolves to the one active Attempt rather than
creating another.
_Avoid_: Analyser, Material Memory, ASTER Run

**Material Consumption**:
Read-only use of one exact Ready Material by ASTER planning or rendering.
Any number of consumers may use the same Material concurrently; consumption
never changes its source or Material Memory and consumers never serialize one
another merely because they share a Material. A consumer holds its shared
consumption lease until its Execution Attempt reaches a terminal state. Material
inspection is not consumption and never blocks deletion. Deletion is rejected
immediately rather than waiting whenever the Material is referenced, has an
active Material Analysis Attempt, or has an active consumer.
_Avoid_: Material Analysis, Material inspection, Material replacement

**Material Inspection**:
User-facing browsing of Material identity, Memory, covers, waveform, or source
media. Inspection never acquires a Material Consumption lease and never blocks
planning, rendering, analysis, or deletion. An inspection that has already
obtained a stable view may finish after deletion; a new inspection after the
delete commits returns Not Found rather than waiting.
_Avoid_: Material Consumption, Material Analysis, Material Reference

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

**Application Data Root** *(Resolution, storage, and migration implemented)*:
The single local directory containing CutMaster's frontend database, Material
Library, managed Project/Run/Frozen Edit/Render Variant artifacts, workflow
receipts, durable job state, and logs. It defaults to `CutMaster/.cutmaster/`
and may be set to a custom local directory; individual data categories do not
use independently configured roots. Repository-level bootstrap configuration,
secrets, and the root-location pointer are outside it.
_Avoid_: browser storage, output directory, Material Library

**Managed Artifact Reference** *(Implemented)*:
A portable reference from a frontend-owned record to managed filesystem data.
It uses a stable owning-entity identity (a Material ID for Material artifacts)
and an Application Data Root-relative path; an absolute Path is resolved only
at a local service boundary.
_Avoid_: absolute persisted path, exported copy, source path selector

**Managed Workflow Receipt** *(Implemented)*:
The portable result returned by `CutMasterApplication.workflows` after a complete
managed execution and retained as `result.json` under the owning ASTER Run. It
identifies the authoritative Project, Run, Frozen Edit, Render Variant, and
Materials, and maps stable logical artifact keys to normalized Application Data
Root-relative paths. It is an index over managed product history, not a second
artifact bundle or an alternative history model.
_Avoid_: standalone workflow bundle, exported copy, absolute artifact path,
RenderPlan

**Benchmark Submission Copy** *(Implemented by Mashup-Benchmark)*:
The evaluation-facing copy of selected artifacts that the Mashup-Benchmark
adapter writes into the benchmark-provided run directory after the managed
workflow succeeds. The source artifacts and their Project/Run/Frozen
Edit/Render Variant history remain authoritative inside CutMaster's Application
Data Root; deleting or moving the submission copy does not alter that history.
_Avoid_: Managed Artifact Reference, Render Variant master, CutMaster output
directory

**Data Root Migration** *(Implemented)*:
The guarded operation for changing a non-empty **Application Data Root**. It
requires that no Execution Attempt is active, pauses dispatch of queued work,
copies and verifies all managed state, and atomically switches roots only after
verification by updating the external root-location pointer. Failure leaves the
original root active. A stable external lease fences persistent readers and
writers across Web, CLI, Benchmark, and workers; during maintenance all business
API reads and writes return a typed 503. Only health, migration status/control,
and static application resources remain available. A successful pointer switch
enters `restart_required`; the next backend start validates and opens the copied
root. The migration never deletes the old Data Root. Cancellation and failure
remove only manifest-owned staging files, preserve unknown destination content,
and restore the original root and queued dispatch.
_Avoid_: editing the path in place, partial move, separate Material migration

**Project Material Set** *(Implemented in managed backend state)*:
The type-specific collection of video or music **Material References** owned by
an **Edit Project**. The first release permits one video and one music Material
while preserving the collection boundary for future multi-source editing.
Saving this set validates that each Material still exists, is Ready, has the
expected type, and is not being deleted. It does not wait for or conflict with
Material Consumers because adding a reference does not change the Material.
_Avoid_: Material Library, single source path, Frozen Edit

**ASTER Run** *(Managed Web planning lifecycle implemented)*:
An immutable planning record within an **Edit Project** that snapshots its
Material References, Creative Brief, and effective non-secret configuration.
Its Execution Attempts represent each try; successful managed completion
records a RenderPlan reference and produces an initial **Frozen Edit**. The Web
Start editing command dispatches this Planners work to an isolated local
subprocess with durable Attempt/job state, heartbeats, and structured A/S/T/E/R
agent milestones. Retry creates a new Attempt for the same snapshot; Resume
requires an Interrupted Attempt with a valid complete-agent-boundary checkpoint;
Run again creates a new immutable Run from the historical snapshot. Safe Run
deletion removes its owned history and artifacts. Runs, Run detail, and Activity
derive their live state and persisted model usage from the same Execution
Attempt projection. Any input change creates another ASTER Run rather than
changing an existing one.
_Avoid_: Edit Project, overwritten plan, Guided Revision

**Execution Attempt** *(Implemented in SQLite-backed job state)*:
One try to complete a Material Analysis, ASTER Run, or Render Variant without
changing that object's inputs or identity. Retrying creates another Execution
Attempt; changing an ASTER input creates another ASTER Run instead. Long-running
Attempts are scheduled by the shared local supervisor and report durable
milestones. Cooperative Stop becomes Interrupted at a safe operation boundary;
Resume may reuse only a validated operation-specific checkpoint.
_Avoid_: ASTER Run, overwrite, rerun with changed inputs

**Local Job Supervisor** *(Implemented)*:
The single local scheduler for managed Material Analysis, ASTER Run, and Render
Variant Attempts. It claims the durable FIFO queue subject to configured
capacity and per-owner serialization, launches isolated workers, records
heartbeats, cooperatively stops work, and recovers orphaned jobs after process
loss. Shared Material Consumption never serializes otherwise eligible jobs;
actual simultaneous execution remains bounded by supervisor capacity and each
owner still has at most one active Attempt.
_Avoid_: ASTER-only dispatcher, browser task runner, distributed queue

**Job Log** *(Implemented for managed Execution Attempts)*:
The append-only UTF-8 log file owned by one durable Job and therefore one
Execution Attempt. Its absolute path is displayed in Material and ASTER Run
detail. Opening the viewer reads only the latest 50 complete lines and then
tails sanitized structured entries over an Attempt-scoped SSE connection;
closing the viewer closes that connection. The retained file, rather than a
worker process pipe, is the authoritative source so completed and restarted
Attempts remain inspectable.
_Avoid_: global event stream, browser-owned process output, Activity history

**ASTER Stage Checkpoint** *(Implemented)*:
A secret- and path-free durable receipt written only after a complete
Arrangement Architect, Story Editor, Timeline Scout, Edit Composer, or Revision
Editor boundary. A replan-pending receipt additionally preserves sanitized
feedback before the next Arrangement Architect pass. Resume validates and pins
one receipt to the new Attempt; it never continues in the middle of an agent,
model call, or media operation.
_Avoid_: instruction-level checkpoint, raw provider response, automatic retry

**Durable Event Stream** *(Implemented)*:
The monotonically ordered SSE projection used by one global browser connection
to observe managed changes. Reconnection supplies `Last-Event-ID`; retained
events are replayed, while an expired cursor yields `resync_required` and a REST
refetch. Resource queries remain the source of truth.
_Avoid_: command channel, WebSocket state store, page-local stream

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

**Candidate Bundle** *(Required for every Frozen Edit)*:
The immutable, integrity-checked managed artifact set that preserves an ASTER
Run's Candidate Space and deterministic Review inputs. A Frozen Edit is valid
only when this bundle is present, passes integrity validation, and contains the
selected candidate for every required Slot. Missing, damaged, or incomplete
bundle state produces `review_artifact_unavailable`; CutMaster never infers a
Candidate Space or falls back to read-only Review.
_Avoid_: RenderPlan, Revision Draft, inferred candidates

**RenderPlan**:
The portable, immutable, frame-exact Renderer contract compiled from accepted
editorial decisions. It identifies the expected Materials and exact source and
output ranges but contains no runtime media paths.
_Avoid_: Frozen Edit, final video, mutable timeline, runtime media binding

**Frozen Edit** *(Implemented in managed backend state)*:
The immutable product-history identity of one accepted edit version, belonging
to one **ASTER Run**, owning exactly one **RenderPlan**, and associated with the
Run's complete, integrity-checked **Candidate Bundle**. An ASTER Run has no
canonical current or final Frozen Edit; any version may parent many **Guided
Revision** versions and may have many **Render Variants** without duplicating
the timeline.
_Avoid_: RenderPlan, final video, mutable timeline, render cache

**Guided Revision** *(Implemented)*:
A user-directed replacement of a non-anchor Slot's selected passage with
another member of its existing **Candidate Space**. It starts from any valid
Frozen Edit in the same ASTER Run, preserves Slot timing,
arrangement constraints, and Story Anchors, and produces a new child Frozen Edit
without changing its source.
_Avoid_: Freeform timeline editing, rerunning ASTER coordination, Revision Editor

**Revision Draft** *(Implemented page-local Web UI state)*:
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

**Exported Copy** *(Implemented browser download concept)*:
A user-downloaded copy of one Render Variant outside the Application Data Root.
It is not project history and CutMaster does not track later moves or deletion
of that copy.
_Avoid_: Render Variant, managed master, new render

**Dialogue Preview** *(Implemented Web product default)*:
The default **Render Variant**, combining background music with the selected
Story Anchors' original dialogue; with no Story Anchors it contains background
music only. Its state is independent of its ASTER Run: failure or permanent
deletion leaves the completed Frozen Edit and Run available without triggering
ASTER or an automatic render.
_Avoid_: Frozen Edit, BGM-only variant, source-audio mix

**Music Profile**:
A Planners-invocation-specific projection of **Music Memory** onto its requested
output duration, used by the **Arrangement Architect** to shape pacing.
Managed ASTER Runs retain it inside their required Candidate Bundle as a
deterministic Review input.
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
> **Domain expert:** No. Web Guided Revision replaces a non-anchor Slot only
> with an existing member of its persisted Candidate Space and atomically
> produces a new Frozen Edit. If its required Candidate Bundle is missing,
> damaged, or incomplete, Review fails with `review_artifact_unavailable`
> rather than inventing a read-only compatibility mode.

> **Developer:** Can two projects using the same song share their Music Profile?
>
> **Domain expert:** They share one Music Memory. Each Planners invocation
> derives its own Music Profile because its requested output duration may
> differ; every managed ASTER Run retains that profile in the Candidate Bundle
> required by its Frozen Edits.

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
> **Domain expert:** No. Every adapter enters the same managed Application use
> case, which creates another immutable ASTER Run and retains the previous
> result and usage.

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
> **Domain expert:** Yes. CLI uses `CutMasterApplication.workflows`, so its
> Project, ASTER Run, Frozen Edit, Render Variant, usage, and receipt are the
> same managed history that Web reads.

> **Developer:** Is the copy in a benchmark run directory CutMaster's canonical
> output?
>
> **Domain expert:** No. Mashup-Benchmark first completes the same managed
> workflow, then copies only the evaluation-required artifacts into its run
> directory. That Benchmark Submission Copy does not replace CutMaster's
> managed history or Render Variant master.
