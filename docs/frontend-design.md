# CutMaster frontend design

This document records the evolving product and interaction design for the
first CutMaster frontend. Domain language remains authoritative in
[`CONTEXT.md`](../CONTEXT.md); durable architectural trade-offs are recorded as
ADRs.

**Implementation status:** the React/Vite client, FastAPI peer adapter,
`cutmaster serve`, SPA packaging, and the managed local Web workspace are
implemented. Projects, the combined Project Setup, Material import/analysis and
Memory Explorer, Activity, Settings/Setup, Review, Render Variants, and Outputs
all use real Application data and commands. A shared local supervisor runs real
Analyser, Planners, and Renderer subprocesses with durable FIFO/capacity
scheduling, per-owner serialization, cooperative Stop, and orphan recovery.
ASTER Retry, complete-agent-boundary Resume, Run again, deletion, and usage are
implemented. Every Frozen Edit requires a complete, integrity-checked Candidate
Bundle; missing, damaged, or incomplete Review data is unavailable rather than
degraded to a read-only compatibility mode. Strict
Render Specifications, automatic Dialogue Preview, durable SSE with replay and
resync, provider connection tests/local writes, structured Activity navigation,
and guarded Data Root Migration are implemented. Persistent notifications, the
Activity log drawer, Direct Bundle bulk cleanup/retention, generated OpenAPI
TypeScript drift CI, multi-user/cloud operation, and broader provider/media
ports remain later work.

## Product boundary

- The first release is a local, single-user web application whose backend runs
  on the same machine.
- Authentication, tenant isolation, cloud object storage, quotas, and
  distributed job execution are outside the first-release scope.
- Material Analysis, ASTER Planners, Renderer, and Data Root Migration jobs run
  in local subprocesses independently of an open browser tab. Review reads and
  Guided Revision validation remain synchronous Application operations; the
  resulting default Dialogue Preview is dispatched asynchronously.

### Display target

The first release is a desktop editing workspace. It supports viewport widths
of 1280 px and above and is primarily designed for 1440–1728 px displays. The
Review player, Slot timeline, and candidate inspector retain their multi-panel
desktop composition rather than collapsing into a compromised mobile layout.

When the viewport is too narrow for safe editing, the application asks the user
to enlarge the window instead of hiding controls or turning the workspace into
a single column. Phone and tablet layouts are outside the first-release scope.

### Visual language and colour mode

The first release supports **Dark**, **Light**, and **System** colour modes and
defaults to **Dark** to preserve CutMaster's professional editing-workstation
character. System mode follows the operating-system preference and reacts to a
preference change while the application is open. Graphite and blue-grey
surfaces keep footage visually dominant in Dark mode; Light mode uses the same
semantic hierarchy with accessible light surfaces rather than inventing a
separate product theme. Product accents carry stable editorial meaning in every
mode:

- violet — narrative alignment;
- orange — emotion and rhythm;
- teal — visual coherence.

MASTER roles appear as restrained letter badges, stage names, and structured
milestones. They are collaborators in an editing workflow, not six chatbots or
six conversational panels. Status never depends on colour alone: every state
also has an icon, label, and where useful a short explanation.

Colour Mode has the canonical values `dark`, `light`, and `system`; it is not
called Theme because CutMaster has one product visual language. The resolved
rendering mode is always either dark or light. The client applies the saved or
system-resolved mode before React renders, preventing a light/dark flash during
startup, and updates the document `color-scheme` accordingly.

Product copy and design documents use **Colour Mode**. TypeScript, CSS, and file
identifiers use `ColorMode`/`color-mode` to match Web-platform naming; this is a
spelling convention, not a second concept.

### Localization

The first release ships with Simplified Chinese (`zh-CN`) and English (`en-US`).
First launch follows the operating-system language, and Settings provides an
immediate language switch. All user instructions, state labels, validation,
errors, and confirmation dialogs use frontend localization keys rather than
hard-coded copy.

Locale and Colour Mode are browser-level presentation preferences. Once the
user makes an explicit choice, the client stores it locally and restores it on
the next launch; neither value belongs to an Edit Project, ASTER Run, SQLite
product history, or `config.toml`. Resetting the locale returns to operating-
system detection, while selecting System colour mode resumes operating-system
appearance tracking. Dates, numbers, costs, and durations are formatted through
the active locale rather than assembled from translated string fragments.

CutMaster's brand and canonical domain names remain in English in both locales:
`MASTER`, `Analyser`, `Planners`, `Renderer`, and the six agent names are not
translated into competing product terms. Chinese explanatory copy may clarify
their responsibility without renaming them.

## Top-level information architecture

The application is project-first. Its primary navigation is:

1. **Projects** — the default landing page and entry point for creative work.
2. **Materials** — the global reusable Material Library shared by projects.
3. **Activity** — background analysis, Planners, and Renderer job status across
   the application.
4. **Settings** — model, concurrency, rendering, and local storage settings.

An Edit Project references Materials from the global Material Library; it does
not create private copies. A Material may also be imported or selected while a
user is working inside a project.

### Navigation shell

A persistent left rail contains **Projects**, **Materials**, and **Activity**,
with **Settings** anchored at the bottom. Activity carries compact badges for
running and failed work. The rail is the only vertical navigation layer.

Inside a Project, a project header and horizontal tabs expose **Project Setup**,
**Runs**, and **Outputs**. Project Setup combines Material selection and the
Creative Brief rather than duplicating them across Overview, Materials, and
Creative Brief tabs. Entering the Review workspace collapses the global rail to
its narrow form, replaces project tabs with a breadcrumb back to the exact Run,
and gives the player, Slot timeline, and candidate inspector the recovered
space. There is no second project sidebar.

### Browser route state

Browser routes and HTTP API routes serve different purposes. Browser routes
encode enough stable UI hierarchy to restore the selected entity, drawer,
modal, and modal tab after refresh or through Back/Forward; the API remains
shallow and reads the projections needed to reconstruct that route. For the
Material Library, one dynamic route family serves both Material Types:

```text
/materials/:type
/materials/:type/:material_id
/materials/:type/:material_id/memory/:tab
```

`:type` is validated as `video` or `music`. Video accepts `timeline`, `story`,
`dialogue`, and `technical` Memory tabs; music accepts `structure` and
`technical`. This shared routing boundary keeps the Music tab navigable without
falling back to the Video workspace.

Project and Review routes use the same deep-link rule:

```text
/projects/:project_id/overview
/projects/:project_id/runs
/projects/:project_id/runs/:run_id
/projects/:project_id/runs/:run_id/review/:edit_id
/projects/:project_id/outputs
```

`/overview` is retained as the stable URL for the **Project Setup** tab. The
legacy `/projects/:project_id/materials` and `/projects/:project_id/brief`
routes redirect to it and are no longer independent pages.

Within Review, the selected Render Variant and Slot do not add another layout
level, so they are represented as `?variant=:variant_id&slot=:slot_id`. A direct
refresh loads the exact Project, Run, Frozen Edit, Variant, and Slot through the
Run and Review read projections. The unsaved Revision Draft remains page-local
and is never serialized into either path or query parameters.

Primary entity and overlay hierarchy belong in the path. Search, sorting,
type-specific filters, and pagination belong in query parameters and are
preserved while opening or closing an overlay. Hover, panel dimensions,
playback position, and an unsaved Revision Draft are ephemeral and never enter
the URL.

Drawers and Memory Explorers are nested layout routes, not overlays that depend
only on temporary router location state. A direct load of a Memory Explorer URL
therefore renders the Material Library, opens the matching Material drawer, and
then opens the requested tab. Back closes the Explorer before the drawer, and
another Back returns to the filtered list.

Meaningful hierarchy changes push browser history: opening a Material drawer or
Memory Explorer, switching a Project tab, entering a Run or Review, and
selecting another Frozen Edit or Render Variant. High-frequency state replaces
the current entry instead: Slot selection, search typing, sorting, and filters.
This preserves an exact refreshable URL without making Back traverse every Slot
click or search keystroke. Query parameters needed by the parent list are
carried forward across pushed overlay routes.

The unsaved-changes guard participates in browser `popstate` as well as in-app
links. If a dirty Revision Draft would be left by Back, Forward, Edit/Variant
selection, or another pushed route, navigation is paused until the user stays,
saves, or explicitly discards the changes.

### Material identity and name collisions

Video and music are separate Material Types. Within each type, Material Names
are unique and no automatic suffix is assigned:

- Name availability is checked before a file is transferred.
- A collision pauses the import and requires the user to use the existing
  Material, enter a different name, or cancel.
- The accepted normalized name is preserved exactly; the system never appends
  `(2)`, `(3)`, or another suffix automatically.
- After the name is accepted, the system creates an opaque `mat_<uuid>` Material
  ID. The record binds ID, type, exact name, and fingerprint; neither the name
  nor fingerprint is used to derive the ID or managed path.
- SHA-256 is calculated and bound to the Material after upload. It verifies the
  managed source but is not compared across Material records for deduplication.
- Equal bytes may therefore exist under different available names. Avoiding
  that duplication is the user's responsibility.

When a name collision occurs, the UI shows the existing Material's name,
thumbnail, duration, and analysis status before any file transfer. It never
overwrites or silently renames either Material.

The collision dialog offers exactly three paths:

- **Use in this project** when importing from an Edit Project, or **View
  existing Material** when importing from the global library.
- **Change name**, which returns focus to the name field while preserving the
  selected local file.
- **Cancel**, which abandons the attempted import.

There is no overwrite, replace, force-upload, or automatically generated name
action.

### Material Library presentation

The Material Library separates **Videos** and **Music** rather than mixing
types. Within either type, fixed-size cards flow left-to-right and top-to-bottom
in a non-masonry grid. Cards remain approximately 272 px wide with a 16 px gap,
yielding roughly three columns at 1280 px, four at 1440 px, and five at 1728 px.
Sorting determines position; a live analysis-state update never causes a card
to jump elsewhere unexpectedly.

Video and music cards share one footprint. Video uses a 16:9 thumbnail, while
music uses a waveform in the same preview region. Each card shows a two-line
Material Name followed by duration, analysis state, and reference count.
Search and sorting sit above the grid; Import remains at the upper right. The
first release has no bulk selection or bulk deletion.

Selecting a card opens a roughly 440 px summary drawer over the right side of
the grid without reflowing cards. The drawer shows the preview, identity,
source metadata, Material Memory summary, Material References, analysis
Attempts, allowed recovery actions, and the guarded deletion action. Its state
is represented by `/materials/:type/:material_id`, so Back and refresh behave
predictably even though Material IDs are not printed in the UI. Fingerprints and
internal paths are also not shown; **Show in Finder** is the filesystem action.

#### Video Memory Explorer

The video summary drawer contains **View full analysis**. This opens a large,
near-full-screen modal over the Material Library rather than navigating to a
separate page. Closing the modal restores the same selected card, scroll
position, and open summary drawer. Browser Back closes the modal before closing
the drawer. Its four tab names are nested URL segments, so refresh restores the
same tab without relying on temporary browser state.

The read-only modal has four tabs:

1. **Timeline** (default) — a Segment/Shot Inspector on the left, source video
   player on the right, and a full-source timeline along the bottom. Segment
   selection seeks the player and exposes its summary, narrative function,
   emotion, appearing characters, dialogue, and child Shots. Shot selection
   exposes visual description, action, scene, character appearances, camera
   grammar, composition, visual evidence, and stored or on-demand sampled
   frames. A provider-rejected visual annotation is shown as unavailable for
   that Shot; it does not mark the whole Material as Failed.
2. **Story** — title, logline, synopsis, chronological Story Beats, Character
   Arcs, themes, and ending. Beat and arc evidence links select the cited
   Segments in Timeline.
3. **Dialogue** — searchable, speaker-filterable, source-ordered dialogue that
   follows playback and seeks to the relevant Segment and Shot when selected.
4. **Technical** — duration, dimensions, frame rate, model identifiers, Shot
   detection settings, annotation completion, and provider-rejected Shot count.

The modal does not expose the Material Fingerprint, managed absolute paths,
prompts, or checkpoint JSON. Analysis Attempt history remains in the summary
drawer instead of being mixed into Video Material Memory browsing.

#### Music Memory Explorer

The music summary drawer uses the same **View full analysis** entry and
near-full-screen modal pattern, but exposes only the structure present in Music
Memory. It has two read-only tabs:

1. **Structure** (default) — a synchronized audio player and waveform overlaid
   with the normalized energy curve, Beat markers, stronger Accent markers, and
   labelled Section intervals. Selecting a Section seeks playback and shows its
   role (`intro`, `build`, `development`, `climax`, `release`, or `outro`), mean
   energy, energy trend, and suggested clip-duration range.
2. **Technical** — source duration, tempo in BPM, Beat, Accent, and Section
   counts, energy sampling interval, schema version, and analysis state.

The music modal does not invent Story or Dialogue views, expose the managed
audio path, or allow Music Memory to be edited.

### Import and analysis lifecycle (implemented)

The confirming action for a new source is **Import & Analyse**. Once the
type-scoped Material Name is available, the frontend sends a multipart upload;
the backend calculates and binds its Material Fingerprint, then immediately
queues the real Material Analysis worker. Video accepts one optional SRT in the
same command. Name collision, invalid file, storage, and subtitle blockers use
stable Problem codes, and the action explicitly warns that analysis invokes
configured models and records usage.

A newly imported Material moves through:

```text
Uploading -> Queued -> Analysing -> Ready/Reused
                         |
                         +-> Stopping -> Interrupted
                         +-> Failed
```

A failed analysis keeps the immutable Material and exposes **Retry Analysis**;
the source is not uploaded again. An Interrupted analysis exposes **Resume**
when its compatible analysis checkpoint is available. Queued work stops
immediately; Running work becomes Stopping until Analyser reaches a cooperative
model/media boundary. The summary drawer exposes Attempt history and the valid
recovery action, and a terminal unreferenced Material may be deleted through
the two-step guarded confirmation. An Edit Project may reference a queued or
analysing Material, but Start editing remains unavailable until its required
video and music Materials are Ready.

Before an operation consumes a Ready Material, the backend verifies its bound
Material Fingerprint. A mismatch changes its condition to **Inconsistent** and
blocks analysis, planning, rendering, and selection for a new Run; recovery is
deleting the unreferenced Material and importing it again.

Video import accepts one optional `.srt` subtitle. When present, it is used to
build the Video Material Memory without ASR; when absent, the import dialog
states that ASR will run. The subtitle selection is locked when analysis starts
and cannot later replace dialogue in a completed Material Memory. Correcting it
requires deleting an unreferenced Material and importing it again. Music import
has no subtitle control.

Ready video cards request a bounded analyser-produced JPEG thumbnail and Ready
music cards request an SVG waveform derived from the stored energy curve. List
and card projections never inline or eagerly download the complete source
video/audio. Missing or invalid preview artifacts degrade to the card fallback
without making the Material itself unusable.

## Projects landing page

Projects appear as a wider card grid, with three columns at the primary 1440 px
target. A card's preview uses its latest completed output frame, falls back to
the selected Video Material thumbnail, and otherwise shows a clear empty state.
It also shows Project Name, a short Editing Intent excerpt, selected video and
music names, latest ASTER Run state, and last-updated time. These fields make
projects with equal display names distinguishable.

The page supports name search and sorting by recently updated, creation time,
or Project Name. **New Project** remains at the upper right. Selecting a card
opens Project Setup; rename and permanent deletion live in the card's
overflow menu rather than on its primary surface. The first release has no
bulk project actions.

## Edit Project workspace

An Edit Project opens as a persistent workspace rather than a transient
wizard. Its first-release tabs are:

1. **Project Setup** — selected video and music Material References, Editing
   Intent, Target Duration, save state, and the Start editing action.
2. **Runs** — ASTER execution status and Frozen Edit results.
3. **Outputs** — implemented rendered Variant status, playback, download,
   Finder, recovery, and permanent-deletion actions.

An empty project completes Material selection and the Creative Brief on the
same Project Setup page. Closing the page does not cancel background work, and
reopening the project restores its last explicitly saved setup and current Run
state.

Every Edit Project has an opaque stable ID and a mutable, non-unique Project
Name. Filesystem layout and relationships use the ID, so renaming never moves
artifacts. New projects require only a name and may begin as `Untitled Project`.
Projects with equal names are distinguished by Material thumbnails, update
time, and recent Run state.

### Project Setup

Project Setup is the only editable home for both the Project Material Set and
the Creative Brief. It presents them as numbered sections followed by a compact
MASTER readiness summary and the Save/Start actions. There is no parallel
Overview dashboard and no editable duplicate of either input.

#### Material selection

Project Setup presents separate **Selected Video** and **Selected Music**
collections. The first release shows at most one item in each collection, but
the component and API boundary remain plural for future multi-source editing.
The selectors draw from the corresponding global Material Type, and an
Inconsistent Material is not selectable. The UI avoids **Replace Material**,
which could imply changing an immutable source file.

Changing the current Project Material Set never mutates or deletes an existing
ASTER Run. A running Run keeps its snapshotted Material References, and the page
states that the changed selection applies only to the next Run. Queued or
Analysing Materials may be selected, but Start editing remains unavailable
until they are Ready. Failed Materials expose Retry Analysis, while
Inconsistent Materials cannot be selected.

#### Creative Brief

The first-release Creative Brief exposes exactly two user inputs:

1. **Editing Intent** — required natural-language direction for the edit.
2. **Target Duration** — required desired output duration.

Project Setup uses explicit persistence. Editing either Material selection or
Creative Brief field marks the page **Unsaved changes** and enables a dedicated
**Save** button; there is no background autosave. Save atomically updates the
mutable Project Material Set and Creative Brief and does not create an ASTER
Run.

Target Duration uses a whole-second `mm:ss` control. A plain integer such as
`90` is accepted and normalized to `01:30`; `30s`, `60s`, `90s`, and `3min`
shortcuts only fill the same field. The value must be positive and must not
exceed the selected Music Material's complete source duration. Both the form
and backend application layer enforce that upper bound even though lower-level
music projection tools can repeat a short track.

Until a Music Material is selected, Target Duration is unavailable and points
the user to the Material section above. Changing to a shorter track revalidates
the last saved value. If it now exceeds the new source duration, CutMaster
preserves the value, marks the Creative Brief **Needs update**, and disables
Start editing until the user corrects and saves it. It never silently clamps
the duration.
Changing to a longer track leaves an otherwise valid Brief unchanged.

While the form is dirty, project-tab navigation, Back, page exit, and **Start
editing** are intercepted by an unsaved-changes warning and the requested
action does not proceed. The user returns to the form and saves before trying
again. Once saved, Start editing creates the immutable ASTER Run snapshot from
the last persisted Brief. It never snapshots uncommitted browser state.

The frontend does not expose `prompt_type`, target shot length, maximum clip
duration, agent retry counts, candidate counts, Beam width, or model controls.
`prompt_type` has no product-level meaning and remains only a compatibility
field for existing CLI and Benchmark calls. Shot pacing and all other agent
controls come from the active system configuration and are snapshotted by an
ASTER Run where relevant.

## ASTER Runs, Review, and Render Variants (implemented)

Every accepted **Start editing** command creates a new immutable ASTER Run. The
Run snapshots its Material References, Creative Brief, and effective non-secret
Planners configuration. Editing the Project after a Run starts never mutates
that Run; starting again creates another Run with an independent managed
artifact directory.

Within one Project, Runs receive a stable creation-order label such as `Run 01`,
`Run 02`, and `Run 03`. One global SSE connection invalidates the lightweight
SQLite projections; bounded polling remains a fallback if that connection is
unavailable. The list, detail page, and Activity derive their visible status
from the same current Execution Attempt while retaining the Run's separate
lifecycle state. Run detail shows Editing Intent, Target Duration, the real
A/S/T/E/R milestone lane, heartbeat freshness, generic localized failure state,
persisted model usage, resulting Frozen Edits, and the valid Retry, Resume, Run
again, Stop, or Delete action. Each Frozen Edit links to its Review route.

One ASTER Run may produce multiple Renderer variants without repeating
material analysis or ASTER coordination. For example, BGM-only and dialogue
versions may share one Frozen Edit and appear as sibling outputs under the Run.
Previous Runs and their outputs remain inspectable and renderable.

The implemented ASTER worker claims the exact durable job, sends heartbeats,
executes the real Planners use case from the Run snapshot, atomically publishes
`projects/<project-id>/runs/<run-id>/plan.json`, and commits that RenderPlan as
the sole immutable timeline payload of a new Frozen Edit. It also publishes the
versioned, integrity-checked Candidate Bundle required by Review and Guided
Revision. The Run becomes `Complete` only after both artifacts satisfy that
contract.
The Web flow then creates and dispatches the default **Dialogue Preview** as an
independent Render Variant. Preview failure never changes the completed Run or
Frozen Edit.

Frozen Edit is the product-history identity and RenderPlan is its exact
Renderer contract. SQLite stores identity, Run ownership, lineage, and a
portable RenderPlan reference; it does not duplicate the timeline as another
writable representation. Direct CLI and Benchmark plans do not become Frozen
Edits. A Frozen Edit records whether it is the Run's `initial` version or a
`guided_revision` derived from an optional parent Frozen Edit. Review state and
all Render Variants attach to this stable version identity, not to a plan file
path or an Execution Attempt.

Neither the Project nor the ASTER Run persists a mutable **current** or
**final** Frozen Edit. Opening Review without an explicit edit target selects
the newest Frozen Edit by its Run-local creation sequence; selecting another
version is navigation state reflected in the page URL, not a change to project
history. A newly applied Guided Revision navigates directly to its child Frozen
Edit and dispatches that child's default Dialogue Preview. Render and export actions always show the
explicit `Edit NN` target, and the first release has no separate **Mark as
final** action.

ASTER Run execution state and Review readiness are separate. The Run is
`Complete` as soon as Planners successfully produces its initial valid Frozen
Edit and required Candidate Bundle.
Its automatic Dialogue Preview moves independently through `Queued`,
`Rendering`, `Ready`, `Failed`, `Interrupted`, or `Unavailable`, while the Run
row presents a useful composite such as **Complete · Rendering preview**,
**Ready for review**, **Complete · Preview failed**, or **Complete · Preview
unavailable**. If the preview is permanently deleted, the Run remains
`Complete` and presents **Complete · No preview**. Retrying a failed Dialogue
Preview or repairing an unavailable one creates another Renderer Execution
Attempt for that same Render Variant; it never reruns ASTER or creates another
Run. The ASTER Run itself is `Failed` only when Planners fails.

### Output retention and export (implemented)

Every completed Render Variant has one managed master file beneath the
Application Data Root. That file remains attached to the immutable project
history and is the source used for in-app playback. The first release does not
move or rename managed masters from the frontend.

The Outputs page provides **Play**, **Create BGM-only Variant**, **Download
copy**, **Show in Finder**, and **Delete permanently**. Creating the BGM-only
Variant always targets an explicit Frozen Edit, defaulting to the one currently
selected in Review or Run details; it never silently targets a different
revision.
Downloading streams a copy through the browser to the location chosen by the
user or browser settings. It may be repeated without creating another Render
Variant or changing its managed path. Copies outside the Application Data Root
are not tracked by CutMaster; deleting or moving an exported copy does not
affect the managed master.

A Render Variant may be deleted independently when all its Execution Attempts
are terminal. Permanent deletion removes that Variant's managed master,
Attempts, jobs, Activity records, logs, and owned artifacts, but preserves its
Frozen Edit and every sibling Variant. Rendering the same audio/settings choice
later creates a new Render Variant identity rather than restoring the deleted
record. Deletion never starts Renderer or creates a replacement output.

Variant creation is idempotent within one Frozen Edit and normalized Render
Specification. Repeating the same request opens an existing Ready Variant or
its current Queued/Rendering progress instead of creating another record. A
Failed Variant exposes **Retry**, and an Interrupted Variant exposes **Resume**;
both actions create another Execution Attempt beneath that same Variant. Only a
different audio mode or output-affecting Renderer setting—or permanent deletion
of the prior Variant—permits a new Render Variant identity.

Each Variant snapshots its normalized non-secret Render Specification at
creation. **Retry**, **Resume**, and **Render again** always use that historical
snapshot even after the repository configuration changes. Output details show
its human-readable audio mode, resolution, and encoder summary. When the
currently loaded settings differ, the UI may additionally offer **Render with
current settings**, which targets a new Render Variant and never mutates or
silently upgrades the old one. The first release has no separate
**Original settings unavailable** condition or workflow; an exceptional
environment incompatibility is shown as an ordinary failed Renderer Attempt.

Before playback, download, Show in Finder, or reuse of a Ready Variant, the
Application verifies that its managed master still exists and passes recorded
integrity checks. A missing or damaged master changes the Variant condition to
**Unavailable**, disables those artifact actions, and exposes **Render again**
and **Delete permanently**. Unavailable is not Failed because no Renderer
Attempt produced the inconsistency. Render again starts a new Attempt beneath
the same Variant; success restores Ready without creating another identity, and
nothing is repaired automatically.

CutMaster does not scan every managed master at application startup. Loading an
Outputs list performs only quick existence and expected-size checks; obvious
problems immediately mark that Variant Unavailable. Every completed managed
master records an internal byte size and SHA-256 in addition to its frame count
and duration. The first playback, download, Show in Finder, or reuse in each
Application process verifies that hash; a process-local cache keyed by the
file's path, size, and modification time skips repeat hashing until the file
changes. These internal integrity values are not exposed in the normal UI.

## Review and Guided Revision (implemented core)

The focused Review workspace places the Slot and Candidate Inspector on the
left, the larger video player on the right, and a synchronized Slot timeline
across the bottom:

```text
┌ Back to Run ───── Frozen Edit / Variant ───── Actions ┐
├───────────────────────┬───────────────────────────────┤
│ Slot / Candidate      │                               │
│ Inspector             │         Video Player          │
│                       │                               │
├───────────────────────┴───────────────────────────────┤
│ Slot Timeline + dialogue waveform + music beats      │
└───────────────────────────────────────────────────────┘
```

The implemented Review projection loads the immutable RenderPlan, version
lineage, Slot sequence, Dialogue cues, music beats when available, any existing
Render Variants, and the required versioned Candidate Bundle containing the
validated Candidate Space and deterministic Review inputs. The bundle must be
present, pass all recorded integrity checks, and contain the selected candidate
for every required Slot. Any missing, damaged, or incomplete Review artifact
returns `review_artifact_unavailable`; the page does not infer or fabricate
candidates and does not open in a read-only fallback mode.

Its canonical browser route is
`/projects/:project_id/runs/:run_id/review/:edit_id`. Optional `variant` and
`slot` query parameters restore secondary selection after refresh without
changing which Frozen Edit is being reviewed. The breadcrumb always links to
the exact Run route rather than relying on browser history alone.

Deleting the selected Render Variant returns Review to the same explicit
Frozen Edit with no Variant selected; it does not silently switch to a sibling.
The player becomes an empty state with **Render Dialogue Preview** and **Create
BGM-only Variant** actions. If the deleted Variant was the only preview, the
rest of the RenderPlan, Slot timeline, and Candidate Inspector remain available
for review, and no render starts until the user requests one.

Selecting a Slot seeks the player to its output interval and updates the left
Inspector. A non-anchor Slot reveals only alternatives already validated in
that Run's persisted Candidate Space, including source time, visual evidence,
and available scores. The Inspector and timeline are expanded by default but
may be collapsed to enlarge the player. Choosing an alternative adds it to an
unsaved Revision Draft held only by the current Review page; it does not call a
draft API, render immediately, or rerun ASTER. An unavailable Candidate Bundle
prevents Review from opening; it never leaves a partially inspectable candidate
view.

Story Anchor Slots are locked during Guided Revision. The first release does
not support freeform reordering, arbitrary source trimming, changing Slot
duration, or inserting footage outside the Candidate Space. A user who needs a
different story structure or anchor set changes the Creative Brief and starts a
new ASTER Run.

Candidate replacements accumulate in a page-local **Revision Draft**. The user
may preview source candidates, undo individual replacements, or reset the draft
without rendering. There is no draft persistence or background autosave, so
refreshing or losing the browser process cannot recover those replacements.
**Save revision** sends the complete replacement set as one command, validates
it atomically, deterministically compiles one new RenderPlan, commits one
immutable derived Frozen Edit, navigates to that child Review, and dispatches
its default Dialogue Preview. It does not rerun ASTER. A validation or compilation
failure creates no partial history and leaves the page dirty so the user can
correct and save again.

While the Revision Draft is dirty, switching Frozen Edit or Variant, returning
to the Run, navigating elsewhere, refreshing, and closing the page are
intercepted by an unsaved-changes warning. For in-app navigation, the modal
offers **Stay on page** and the explicit destructive action **Discard changes
and leave**; it never silently follows the original navigation. Choosing the
latter clears the page-local replacements and then resumes that exact action.
Browser refresh or close uses the platform's unload confirmation. A successful
**Save revision**, **Reset changes**, or explicit discard returns the page to a
clean state. The source Frozen Edit always remains unchanged.

Guided Revision may start from any valid Frozen Edit in the same ASTER Run,
including one that already has newer descendants. Frozen Edits receive stable
Run-local creation labels such as `Edit 01`, `Edit 02`, and `Edit 03`; each
derived entry also shows **Based on Edit 01**. The first release presents these
versions as a creation-ordered list with parent labels rather than a graphical
version tree, but it preserves the branching parent relationship and never
overwrites or hides a sibling branch.

## Activity and progress

Long-running work is represented as structured milestones rather than an
estimated overall percentage. The managed ASTER worker persists real
Arrangement Architect, Story Editor, Timeline Scout, Edit Composer, and
Revision Editor boundaries in Job progress. Material Analysis shows the real
Material Analyst operation and bounded counts when available; Render Variants
show Renderer as a separate lifecycle. Run detail and Activity observe the same
Attempt/Job projection through global SSE with a polling fallback.

Each milestone has one of `Queued`, `Running`, `Retrying`, `Stopping`,
`Interrupted`, `Complete`, `Failed`, or `Reused`. It shows the current operation
and elapsed time, and may show a real bounded count such as annotated Shots
when the backend provides one. Cache hits appear as `Reused`, not as instant
fresh analysis. The same job state is visible in its Project or Material
context and in the global Activity page. Failure details expose attempt history
and the allowed recovery action without presenting an invented completion
percentage. User-facing failure details use stable, localized operation/status
copy; raw persisted worker exception text is not rendered in Material, Run,
Render, or Activity UI.

The global Activity page groups work by immediate user relevance:

1. **Running** — the current Attempt expanded with milestones, elapsed time,
   current operation, and Stop Attempt.
2. **Queued** — waiting work with FIFO position and its waiting reason.
3. **Needs attention** — Failed and Interrupted work with Retry or Resume.
4. **Recent** — recently Complete or Reused work.

Unavailable Render Variants never appear in these groups because no Execution
Attempt produced that condition. Their Run and Output cards carry an
**Unavailable** warning badge; **Render again** creates a Renderer Attempt that
then appears in Activity normally.

Every row names the actual object, for example `Material Analysis · La La
Land`, `Run 03 · Project Name`, or `Dialogue Preview`; it never displays a
generic Task. The implemented Activity projection supplies structured owner
navigation context, and selecting a row goes directly to the exact Material,
Run, or Render Variant context without making the client guess missing parent
IDs. A richer right-side drawer with Attempt history and concise logs remains
Proposed. The main list does not expose the low-level event stream. **Load
more** advances the Activity cursor instead of silently truncating history at a
fixed first page.

Activity history is retained with its owning domain object and disappears only
through that object's defined deletion cascade. The first release has no
standalone **Clear history** command. Older records use paginated loading.

### In-app notifications (Proposed P2)

Persistent application notifications described in this section are not yet
implemented. The intended local product uses application notifications rather
than requesting browser or operating-system notification permission. Complete work produces a
short success toast. Failed or Interrupted work produces a persistent notice
with the valid Retry or Resume action, while the Activity badge retains running
and unhandled-failure counts.

Detecting an Unavailable Render Variant creates one deduplicated persistent
application notice linked to that exact Variant, with **Render again** and
**Delete permanently** actions. It does not increment the Activity badge or
create an Activity record; the warning remains visible on the owning Run and
Output even when no rendering work is active.

The notice may be dismissed as **Acknowledged**, but acknowledgement never
changes the Variant's Unavailable condition or removes its Run/Output warning.
The same Unavailable episode does not generate another notice. Returning the
Variant to Ready or deleting it resolves the notice automatically; if a repaired
Variant later becomes Unavailable again, that new condition transition may
create one new notice.

Closing the browser never stops backend work. On the next launch, CutMaster
summarizes unseen completions, failures, and interruptions that occurred while
the UI was closed and links each item to its owning object. There are no email,
push, or closed-browser desktop notifications in the first release.

### Failure and retry identity

Retrying unchanged inputs creates another **Execution Attempt** under the same
domain object:

- Material Analysis resumes for the same Material and may reuse checkpoints.
- A Planners-failed ASTER Run retains its immutable snapshot and Attempt
  history; Retry creates another Execution Attempt for that Run.
- Renderer failure does not fail the owning Run; Retry creates another Attempt
  for the same Render Variant.
- A failed Dialogue Preview does not remove the Frozen Edit that requested it.

Changing a Material Reference, Editing Intent, or Target Duration creates a new
ASTER Run instead. Successful Runs expose **Run again**, failed work exposes
**Retry**, and interrupted work exposes **Resume**.

### Stop and resume

Running work exposes **Stop Attempt**. Queued work stops immediately; active
work enters `Stopping` and stops after the current model call, media operation,
or other safe checkpoint. Logs, usage, intermediate artifacts, and reusable
checkpoints are retained. The terminal status is `Interrupted`, never `Failed`.

**Resume** creates another Execution Attempt under the same Material Analysis,
ASTER Run, or Render Variant and reuses safe work. `Failed` is reserved for
internal errors and exhausted recovery. Stopping never deletes the parent
domain object or its artifacts.

For ASTER, safe work means only an identity-bound checkpoint after a complete
A/S/T/E/R agent boundary, plus an internal replan-pending checkpoint that stores
sanitized feedback before the next Arrangement Architect pass. It never means
continuing in the middle of an agent instruction, provider request, or media
operation. Retry starts the immutable Run snapshot without claiming this partial
continuation.

## Deletion and Material References

A Material may be deleted only when it has no Material References. The
application checks both mutable project selections and immutable ASTER Run
snapshots. A blocked deletion lists each Project and exact Run with navigation
links; there is no force-delete action.

Deletion also requires every owned Execution Attempt to be terminal. A
Material with queued, running, retrying, or stopping analysis cannot be deleted;
the same rule covers Planners and Renderer Attempts beneath a Run and every
nonterminal Attempt beneath a Project. The confirmation identifies each
blocker. The user explicitly stops it, waits for `Interrupted`, and then starts
deletion again. Delete never substitutes for Stop or kills a subprocess.

The user releases references manually:

1. Remove the Material from every current Project Material selection.
2. Explicitly delete each ASTER Run whose snapshot references the Material.
3. Deleting a Run cascades to its Frozen Edits, Render Variants, Execution
   Attempts, Activity records, jobs, logs, and artifacts, but does not delete
   its Edit Project. There is no persisted Revision Draft to delete.
4. Delete the Material only after the reference count reaches zero.

The UI never calls these objects generic Tasks. It names Material Analysis,
ASTER Run, Execution Attempt, and Render Variant separately.

A Frozen Edit cannot be deleted independently because it may be part of a
revision branch and remains the editorial history shared by multiple Render
Variants. It is removed only by deleting its owning ASTER Run. A Render Variant
is independently deletable, but a queued, running, retrying, or stopping
Renderer Attempt blocks deletion until the user stops it and it reaches a
terminal state.

### Permanent deletion confirmation

The first release has no Trash. Deleting an ASTER Run permanently removes its
entire Run-owned state; deleting an Edit Project permanently removes its Runs
and all Project-owned records but leaves global Materials; deleting an
unreferenced Material permanently removes its managed source, Material Memory,
Material Analysis Attempts, jobs, Activity records, logs, and artifacts.
Deleting a Render Variant removes only that output realization and its owned
state; it never deletes the Frozen Edit or an Exported Copy.

Deletion requires two clicks but no typed-name challenge. The first click opens
a confirmation dialog listing the cascade and relevant storage size; the
second click is an explicit destructive **Delete permanently** action.

Direct Workflow Bundles are never deleted automatically and do not appear as
Project or Activity records. Settings → Storage shows their count and total
size and provides **Open in Finder**. **Delete all direct bundles**, active-lock
skipping, and any automatic retention policy remain Proposed P2 work.

## Settings and model setup

Settings manages product-level model connections for the local application. It
offers presets for **Cost Saving** (DeepSeek LLM with DashScope VLM and ASR),
**Simple** (all services through DashScope), and a custom OpenAI-compatible
connection. First launch routes to Setup when a required credential is missing.
Setup and Settings both persist the selected provider profile through the real
Application service and offer bounded, per-capability LLM/VLM/ASR connection
tests rather than a simulated success state.

Settings also contains an **Appearance** section for the immediate `zh-CN` /
`en-US` language switch and `Dark` / `Light` / `System` colour-mode selection.
These controls update browser presentation preferences and do not submit a
managed Application command.

API keys are written only to the sibling local `.env` file and are never stored in
browser storage, logs, project records, or ASTER Run snapshots. After saving,
the frontend reveals only configured state, source, writability, and a short
suffix. Secret fields support keep/set/clear semantics; the Application writes
the file atomically with owner-only permissions and preserves process-environment
precedence. Separate connection tests cover LLM, VLM, and ASR.

The repository-level `.env` and `config.toml` are bootstrap configuration, not
Application Data Root contents. They remain in the CutMaster project directory
when managed user data moves. A local, git-ignored `.cutmaster-location` file in
that same directory contains only the absolute location of a custom Data Root;
when it is absent, CutMaster resolves the default `CutMaster/.cutmaster/`.

Settings never rewrites the version-controlled `config.toml`. Editable
non-secret model, endpoint, timeout, concurrency, rendering, and execution
values are stored as a sparse, git-ignored `config.local.toml` overlay beside
the base file. API keys remain in the git-ignored `.env`. Before either local
file is atomically replaced, the Application validates the complete effective
configuration produced by merging the base and overlay. CLI, Benchmark, Web,
and workers all load that same effective result through
`CutMasterApplication.open(config_path)`.

For an explicitly selected base such as `/path/eval.toml`, the local overlay is
only `/path/eval.local.toml`; absence of that file means base-only configuration.
CutMaster never applies the repository `config.local.toml` to another selected
base. Settings reads and writes the sibling of the base used to launch the Web
application, keeping evaluation and interactive configuration scopes isolated.

Model names, endpoints, timeouts, and concurrency are editable global settings.
Research controls such as agent retry counts, candidate counts, Beam width,
shot-detection thresholds, and scoring thresholds remain in `config.toml` and
are not exposed as first-release forms. ASTER Runs snapshot effective
non-secret configuration where needed for reproducibility.

A successful Settings save applies only to subsequently created work. Existing
Material Analyses, ASTER Runs, Render Variants, and all their Queued or Running
Attempts retain their captured non-secret specification; Retry and Resume create
new Attempts under that same snapshot. API keys are not snapshotted: a new
Attempt resolves them when it starts, allowing credential repair without
silently changing models, thresholds, or rendering behaviour. The UI explains
that changing settings never reconfigures active or historical work.

## Technical architecture

**Status: Implemented over the Application Layer, including managed Material
Analysis, ASTER planning, Renderer/Outputs, shared supervision, durable SSE,
provider Setup, Activity navigation, and Data Root Migration.**

The first frontend uses React, TypeScript, and Vite as a client-side
single-page application. The existing framework-independent CutMaster
Application Layer already exposes domain commands and queries to CLI and
Benchmark. FastAPI is the Web peer adapter: REST handles commands and
authoritative queries, while one global Server-Sent Events connection carries
durable invalidation and progress events. Bounded REST polling is the fallback
when SSE is unavailable. The local application is
launched through `cutmaster serve` and opens in the user's
browser. It will not use server-side rendering.

The repository, Python package, React client, test, and generated-resource
layout is defined in
[`architecture.md`](architecture.md#repository-layout-and-web-expansion).
The React project is rooted at `web/`; the FastAPI adapter belongs at
`src/cutmaster/adapters/web/`.

All inbound adapters obtain use cases from one `CutMasterApplication`
composition entry point. The object groups `direct`, `materials`, `projects`,
`runs`, `renders`, `jobs`, and `settings` services; it wires their dependencies
but does not implement their business rules. The CLI and Benchmark worker
already map transport input and output to those services. FastAPI route
handlers follow the same rule and never construct Workflow stages or
infrastructure independently.

The implemented `settings` service owns Effective Configuration reads,
validated local-overlay saves, and storage reporting. Model-connection UI,
credential writes, per-capability connection tests, and the macOS Data Root
Finder action are implemented. Direct Bundle bulk cleanup/retention are
proposed additions; Data Root Migration is implemented. Locale and
Colour Mode remain browser presentation preferences and therefore do not call
this Application service.

Every implemented adapter calls `CutMasterApplication.open(config_path)` and therefore
resolves the same Application Data Root and Material Catalog. Service groups
are initialized lazily: direct CLI and Benchmark calls do not initialize
unrelated managed services. The `serve` command activates the services required
by the Web workspace, including the shared supervisor and Material Analysis,
ASTER, Renderer, and Data Root Migration workers. Review reads and Guided
Revision validation are synchronous Application operations outside the
supervisor; automatic preview rendering is dispatched as a managed Renderer
Attempt after the Frozen Edit commit.

### Web adapter contract

The React client reads noun-based resources through `GET` endpoints, including
collections, detail projections, media streams, logs, and paginated history.
State changes use explicit semantic commands such as Save Project Setup,
Start editing, Retry, Resume, Stop, Save revision, Create Variant, and Delete
permanently. The API does not expose generic database CRUD or a universal
`/commands` endpoint, so each request and response matches one Application use
case rather than a persistence table.

FastAPI validates and translates transport DTOs but contains no domain rules.
Long-running commands return their created or updated domain identity and
Execution Attempt/job reference without holding the HTTP request open. SSE is a
read-only stream of durable state and progress events; the browser never sends
commands over that channel. CLI and Benchmark remain peer adapters and do not
call these HTTP routes.

Every state-changing request has a browser-generated UUID `command_id`, carried
as the HTTP `Idempotency-Key` and mapped into the Application command. Automatic
network retries and a duplicated submission reuse the same ID and receive the
first durable result, even after a server restart. A later intentional action,
including another domain Retry or Resume, receives a new ID. Reusing one ID
with a different canonical payload is rejected as an idempotency conflict
rather than guessed or executed.

Managed long-running commands return `202 Accepted` with their domain, Attempt,
and job projections; each `Idempotency-Key` maps to a durable Application
command receipt. SSE events carry the same correlation values.
Buttons become disabled after the first click, but correctness does not depend
on the browser successfully disabling a double-click.

Every unsuccessful HTTP response uses `application/problem+json`. In addition
to RFC Problem Details fields, it carries a stable machine `code`, safe message
parameters, the relevant `command_id`, optional structured `field_errors` or
typed `blockers`, and `retryable` where applicable. The frontend selects
localized `zh-CN` or `en-US` copy from `code` and parameters; `title` and
`detail` are safe fallbacks for unknown codes, not hard-coded UI copy. Stack
traces, secrets, internal paths, and raw provider responses never reach the
browser.

HTTP status codes preserve their meaning: malformed or invalid input is 4xx,
missing resources are 404, state/name/idempotency conflicts are 409, and an
unavailable service may be 503. A command that already returned `202 Accepted`
reports later execution failure through its Attempt and resource projections,
not a second HTTP response; SSE normally shortens the refresh latency.

#### SSE update transport (implemented)

Each running SPA opens exactly one global SSE stream for all Projects,
Materials, Runs, Attempts, and Variants; pages never create their own streams.
Every durable event has a monotonic `event_id`, schema version, type, timestamp,
typed object reference, and applicable `command_id`, `attempt_id`, and `job_id`.
Events update progress and invalidate cached queries, while resource `GET`
responses remain the source of truth.

The client reconnects with `Last-Event-ID` and receives retained later events in
order. If the cursor is older than the event-retention window, a
`resync_required` event makes the client refetch the current page and Activity
before resuming normal updates. Navigating between pages does not tear down the
global connection.

Resource URLs remain shallow. Owner-scoped collection routes list or create
children, while entity details and commands use the entity's canonical ID:

```text
PUT  /api/projects/{project_id}/setup   # implemented atomic setup save
POST /api/projects/{project_id}/runs    # implemented Start editing
GET  /api/runs/{run_id}                 # authoritative projection / polling fallback

POST /api/runs/{run_id}/retry                    # implemented
POST /api/runs/{run_id}/resume                   # implemented
POST /api/runs/{run_id}/run-again                # implemented
GET  /api/frozen-edits/{edit_id}/review            # Implemented
POST /api/frozen-edits/{edit_id}/revisions         # Implemented atomically
POST /api/frozen-edits/{edit_id}/render-variants  # implemented
```

The frontend never constructs deep routes containing the entire ownership
chain. The backend still verifies every scoped parent-child relationship;
global IDs are addressing tools, not permission to ignore ownership.

Page-level read projections avoid making React assemble one screen from many
small entity requests while remaining resource-oriented:

```text
GET /api/projects/{project_id}/workspace
GET /api/runs/{run_id}
GET /api/frozen-edits/{edit_id}/review             # implemented
GET /api/activity?cursor=...
```

Material Library refresh is intentionally composed from one list projection
and one selected detail query, which may run in parallel:

```text
GET /api/materials?type=video&search=...&sort=...&cursor=...
GET /api/materials/{material_id}
GET /api/materials/{material_id}/memory/{tab}   # lazy heavy view
```

The browser route supplies the selected ID to the loader; it is not necessary
for the list projection to embed every drawer or Memory Explorer payload.
Memory views, logs, full candidate collections, and other heavy data remain
lazy and paginated. Thus a page receives a coherent lightweight snapshot while
deep links still restore one exact item.

### Frontend state ownership

State has one owner according to its lifetime:

- React Router owns refreshable navigation state in paths and query parameters.
- TanStack Query owns all server resources, page projections, pagination,
  request status, and cache invalidation. Query keys use canonical entity IDs
  and normalized filters, never Material Names or filesystem paths.
- Component state or a focused `useReducer` owns unsaved Project Setup fields,
  the page-local Revision Draft, and temporary modal interaction.
- Small React contexts own only application-wide facilities such as locale,
  colour mode, and the one shared SSE connection.
  Locale and colour-mode providers restore their browser-local preferences;
  they never duplicate server-owned product state.

SSE events invalidate or narrowly refresh the
corresponding TanStack Query keys; they will not copy durable entities into
another client-side store. Unsaved forms and revisions never enter the query
cache. The first release uses neither Redux nor Zustand, and introduces a global
state library only if a later feature has state that demonstrably fits none of
these owners.

The Application `materials` service is the only owner of Material lifecycle
operations. It uses a `MaterialCatalog` port whose local implementation owns
the manifest, managed files, fingerprints, and leases. The Workflow Analyser
receives an already resolved Material and owns only analysis and Material
Memory; it does not import, name, resolve, or delete library entries.

Application code resolves the canonical Material into a runtime-only
`MaterialRuntimeHandle` containing identity, fingerprint, and absolute source
and memory paths while it holds the Material lease. Analyser accepts only that
handle, revalidates source consistency, and decides whether existing Material
Memory is compatible. Raw upload paths and candidate names never cross into the
Workflow contract.

Planners likewise receives only analysed video and music runtime handles,
along with a `PlannersBrief` and `PlannersOptions`. The handles bind each Memory
to the exact leased Material; Planners never resolves paths or public names.
The managed Creative Brief maps only Editing Intent and Target Duration into
`PlannersBrief`, while advanced CLI/Benchmark compatibility fields remain in
the separate options object and are not exposed by the frontend.

The existing `Analyser`, `Planners`, and `Renderer` implementations live under
one `cutmaster.workflow` package after the refactor. Their stage names and
public root-package class import paths remain unchanged. Their v2 request
contracts are handle-only and intentionally break the old raw-path Python DTO
signatures; no dual-mode stage wrapper is retained. CLI and Benchmark callers
keep their file/name-facing compatibility through `app.direct`, which resolves
Materials and constructs valid stage handles. The package distinguishes the
CutMaster Workflow from `domain`, `application`, `adapters`, and
`infrastructure` without adding another layer inside each stage.

SQLite stores Projects, Material References, ASTER Runs, Execution Attempts,
Frozen Edits, Render Variants, and durable job metadata. Revision Drafts remain
only in the current Review page and are never written to SQLite or the managed
filesystem. Source media, Material Memory, logs, checkpoints, plans, previews,
and managed render masters remain on the filesystem and are referenced from
SQLite; binary media is never stored in the database.

### Portable managed references

Canonical frontend-managed records never persist a Data Root absolute path.
Projects, Runs, Frozen Edits, Render Variants, jobs, artifacts, and Materials use
stable entity IDs plus paths relative to the active root. Canonical Material
relations use Material ID; `(Material Type, exact Material Name)` remains only
the CLI/core public lookup boundary. Frontend API commands, URLs, and database
relations carry Material ID even though the UI displays Material Name. Material
paths derive as
`media/<type>/<material-id>/...`.
The application layer resolves those references to absolute `Path` values only
while invoking Analyser, Planners, Renderer, media streaming, or local file
operations.

This portable representation is the source of truth. Workflow result files
that require absolute runtime paths are treated as Execution Attempt artifacts,
not canonical project references; rerendering resolves a fresh runtime request
from the Frozen Edit. The historical `.cutmaster/materials-backup/` library is never
scanned or adopted, and pre-refactor workflow output directories are not imported.
Both remain unchanged and outside new project history and Data Root migration.

The portable RenderPlan stores Material IDs and expected fingerprints rather
than source paths or mtimes. For each Render Variant, the Application resolves
those Materials under lease and creates runtime-only `RenderRuntimeBindings`;
Renderer receives those bindings separately from the immutable plan. Explicit
CLI plan rendering goes through the same resolution step for portable v2 plans.
Refactored rendering rejects v1 absolute-path plans with an unsupported-version
error; those historical files are neither migrated, rewritten, nor deleted.

### Application data root

**Implemented:** default/custom root resolution, managed SQLite/Material/
Project/Direct Bundle/log namespaces, Settings UI, and guarded migration of a
non-empty root.

The frontend keeps its managed local state under one **Application Data Root**.
The default is `.cutmaster/` inside the CutMaster project directory, preserving
the current repository-local behaviour. Settings also lets the user select a
custom directory, including a directory on another local disk.

All frontend-owned paths are derived from that root rather than from the
browser or the current page:

```text
CutMaster/.cutmaster/                 # default Application Data Root
├── cutmaster.db                      # product and durable job metadata
├── media/                            # canonical Material Library
│   ├── video/mat_<uuid>/             # managed source + Video Material Memory
│   └── music/mat_<uuid>/             # managed source + Music Memory
├── projects/<project-id>/            # Run, Frozen Edit, and render artifacts
├── direct/bundle_<uuid>/              # non-project Direct Workflow Bundles
└── logs/                              # application and job logs
```

The Settings page shows the selected path and, on macOS, exposes a real **Open
in Finder** action backed by the fixed Application Data Root. A custom path
change moves the whole Application Data Root; Materials, project state,
job state, and artifacts cannot be assigned unrelated roots independently.
The Storage section separately reports database, Materials, Projects, Direct
Workflow Bundles, and logs so non-project CLI output remains visible as disk
usage without appearing in product history.

The implemented migration flow exposes a **Move CutMaster Data** operation in
Settings. After confirming no Attempt is active,
migration pauses queued-job dispatch, copies the database and every canonical
managed filesystem artifact—including manifest-referenced `media/` data—to the
destination, verifies the copied state, and only then atomically switches the
external `.cutmaster-location` pointer to the new root. Historical
`materials-backup/` content is explicitly excluded.
Until that final switch, the original root remains authoritative. A cancelled
or failed migration continues using the original root and reports what must be
fixed; it never leaves the application pointed at a partial copy.

Migration never interrupts an Execution Attempt implicitly. It may start only
when no Attempt is `Running`, `Retrying`, or `Stopping`; the user may wait for
active work to finish or explicitly stop it first. Queued jobs remain queued
while the supervisor pauses dispatch, move with the durable database, and
resume from the same queue after a successful switch. If migration fails, they
remain queued under the original root.

For the duration of the copy and verification, CutMaster enters global
maintenance. All business API reads and writes are blocked immediately with a
typed 503 so no screen can show stale state from the generation being copied;
health and migration status/control remain available for the persistent
progress surface. Success switches the pointer and requires a safe backend
restart before business APIs reopen. Cancellation or failure exits maintenance
against the unchanged original root and restores queued dispatch and all
operations.

Migration never deletes the old Data Root after a successful switch. The old
copy remains for deliberate manual recovery/cleanup; CutMaster does not expose
an automatic old-root deletion action.

### Direct and managed execution

The Application Layer implements two boundaries. Managed product services
create durable SQLite domain records and Execution Attempts. The Web adapter
drives Project, Material import/analysis, Project Setup, Activity,
Settings/Setup, ASTER Run, Review, Render Variant/Outputs, and Data Root
Migration use cases. Its long-running commands dispatch real Analyser,
Planners, Renderer, or migration subprocesses through the shared supervisor.
Frozen Edit Review and atomic Guided Revision validation are synchronous;
automatic Dialogue Preview dispatch is managed asynchronously. The synchronous
Direct surface preserves the `analyse`,
`analyse-music`, `plan`, `render`, and `run` CLI commands without implicitly
creating Project or SQLite history.

`cutmaster run` is the stable complete-generation CLI for evaluation. One
invocation synchronously analyses or reuses both Materials, plans the edit,
renders the final video, writes the workflow Artifact Manifest, and exits with
a machine-readable result. It uses the same direct workflow use case as the
Benchmark adapter and never depends on the Web server, product database, or
durable queue.

When `--output-dir` is omitted, the Application allocates a unique Direct
Workflow Bundle under `direct/` in the active Data Root. It is included in
storage totals but is not Web project history; Data Root Migration moves it
with the root. An explicit CLI output directory must be outside
the Data Root and remains external. Managed artifacts are allocated only from
an owning Project/Run/Variant identity by a managed use case; a CLI path can
never impersonate one.

The implemented CLI consists of the five Direct commands `analyse`,
`analyse-music`, `plan`, `render`, and `run`, plus `serve` for the local Web
workspace. Project, ASTER Run, Activity, and other managed product operations
are Web-only in the first UI release. Their Application use cases remain
independent of FastAPI so managed CLI adapters can be added later without
redesigning them.

Mashup-Benchmark is an implemented peer adapter alongside CLI and FastAPI. Its
integration keeps the per-task worker subprocess but calls
the synchronous direct-execution Application API rather than calling the CLI or
Web adapter. The implemented worker directly calls
`CutMasterApplication.open(...).direct.execute_workflow(...)`. CLI and
Benchmark share that direct Application use case. Neither path requires an HTTP
server or product SQLite history, and both preserve the current three-stage
artifact layout.

The direct result includes a versioned logical Artifact Manifest so the
Benchmark adapter does not need to infer internal paths. Manifest v1 uses stable
dotted keys and normalized POSIX paths relative to the Direct Workflow Bundle;
absolute paths, parent traversal, directory entries, and symlink escapes are
invalid. Required references are validated before `result.json` is atomically
published, while optional missing artifacts are omitted rather than set to
`null`. The result also supplies an explicit stable Material Name from benchmark
media metadata, falling back to the existing local filename stem when metadata
omits one. This preserves cross-task and cross-run Material Memory reuse without
making the fingerprint a selector.

### Durable local job execution

The Application persists managed Attempts, jobs, stop flags, heartbeats, state
transitions, command receipts, and structured events in SQLite. One
`LocalJobSupervisor` claims the FIFO queue under a configured heavy-job capacity
and per-owner serialization rule, launches isolated Material Analysis, ASTER,
and Renderer workers, and reconciles live process leases and orphaned jobs
without duplicate launches. FastAPI never performs model or rendering work
inside the HTTP request.

Workers record internal exceptions as `Failed` and cooperative stop as
`Interrupted`. Analyser and Planners check cancellation at safe model/media or
agent boundaries. ASTER Resume pins a validated secret-free checkpoint after a
complete A/S/T/E/R boundary or the internal replan-pending boundary; it never
continues in the middle of an instruction. Global SSE replays durable events
with `Last-Event-ID`, emits `resync_required` for an expired cursor, and uses
REST projections as authoritative recovery. No Redis, Celery, or external queue
service is required for this local single-user implementation.
