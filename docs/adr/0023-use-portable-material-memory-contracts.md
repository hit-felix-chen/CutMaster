# Use portable, single-version Material Memory contracts

**Status: Implemented.**

Material-owned analysis documents are reusable domain memory, not serialized
runtime bindings. They therefore persist Material identity, content-derived
fingerprints, semantic analysis, and portable artifact references only.
Absolute source paths, Material directories, cache paths, mtimes, and complete
`MaterialRuntimeHandle` values are runtime concerns and are never written into
Material Memory.

The current contracts are intentionally single-version:

| Document | Current schema |
|---|---:|
| Video Memory (`video_description.json`) | `3.0` |
| Music Memory (`music_memory.json`) | `2.0` |
| Analysis result receipt | `3.0` |
| Analysis spec and manifest | `3.0` |
| Dialogue Memory | `2.0` |
| Scene-frame manifest | `2.0` |

Video Segments are identified by `segment_id`; their cached media is resolved
only at runtime as `Material Memory/segments/<segment_id>.mp4`, with containment
and regular-file checks. Scene frames persist filename-only references and are
resolved inside their owning frame cache. Analysis result receipts persist the
Material ID and fingerprint but are rebound to an Application-issued runtime
handle when loaded.

There are no dual readers, fallback field names, or cross-schema checkpoint
reuse paths. A document with an older schema or extra legacy path field is
rejected. Existing managed data was upgraded once, in place, under the Data
Root, catalog, and per-Material locks. The migration backed up every rewritten
JSON document and verified that all source media, Segment clips, frame images,
subtitles, cover images, and derived audio retained identical SHA-256 digests.

This makes a Material store relocatable without reanalysis and prevents local
filesystem topology from entering prompts or resumable ASTER checkpoints. The
cost is an intentional breaking change for old persisted analysis documents;
they must be migrated before the strict runtime opens them.
