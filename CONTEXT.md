# CutMaster

CutMaster models long-form video montage creation as a MASTER Editing Team whose
specialized editorial agents progressively turn source material into a finished
edit.

## Language

**MASTER Editing Team**:
The complete CutMaster team: one **Material Analyst** followed by the five-agent
**ASTER Planning Team**.
_Avoid_: Planner, pipeline, six-stage chain

**ASTER Planning Team**:
The planning team formed by the **Arrangement Architect**, **Story Editor**,
**Timeline Scout**, **Edit Composer**, and **Revision Editor**.
_Avoid_: Planner, planning module

**Editorial Agent**:
A role-bounded decision maker that owns one editorial responsibility and may use
model reasoning, deterministic tools, or both.
_Avoid_: LLM call, prompt stage

**Material Analyst**:
The M agent that converts source footage into reusable **Material Memory** before
edit-specific planning begins.
_Avoid_: Analyser stage, preprocessing

**Arrangement Architect**:
The A agent that defines the montage's Slot arrangement, pacing, emotional
progression, and narrative structure.
_Avoid_: Slot Planner, Rhythm Planner

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

**Material Memory**:
The reusable, edit-independent account of the source footage, including its
Shots, Segments, dialogue, visual evidence, and story summary.
_Avoid_: Video cache, preprocessing output

**Slot**:
One planned interval on the output timeline with an editorial purpose, target
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

**Music Profile**:
A request-specific account of the BGM's beats, accents, energy, and sections
used by the **Arrangement Architect** to shape pacing.
_Avoid_: Material Memory, audio preprocessing

**Production**:
The deterministic realization of a revised edit through source-window
adaptation, dialogue preparation, encoding, assembly, and final audio mixing.
_Avoid_: Editing Agent, planning tool

## Flagged ambiguities

**Editor**:
In this domain, **Story Editor** owns narrative anchoring, **Edit Composer** owns
sequence assembly, and **Revision Editor** owns final review. The unqualified
term “Editor” should not name an agent.

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
