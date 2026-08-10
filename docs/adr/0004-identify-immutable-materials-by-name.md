# Identify immutable Materials by unique names

The Material Library identifies each source video or music asset by a stable,
user-visible Material Name rather than a content hash or source path. The
candidate name defaults to the source filename stem and may be supplied
explicitly.

When another file is added under the same candidate-name family, equal SHA-256
bytes reuse the existing Material and its completed analysis. Different bytes
allocate the next public name, such as `Film (2)` or `Film (3)`, without
replacing existing content. Equal bytes proposed under different candidate
names remain distinct Materials because their public identities are different.

Each Material records SHA-256 only as an internal Material Fingerprint. It is
not embedded in the Material Name, exposed as a selector, or treated as a
global deduplication key. CLI and service callers select completed Materials by
exact Material Name. A fingerprint mismatch marks the Material as inconsistent
and blocks analysis, planning, and rendering; recovery requires deleting and
adding it again because source replacement is unsupported.
