# Model Edit Project Materials as extensible collections

**Status: Proposed — project persistence is not implemented.**

The future Edit Project model will represent its video Materials and music
Materials as two collections of Material References. The current backend has
no Edit Project contract and accepts exactly one video Material and one music
Material in each workflow request. Keeping the proposed project contract plural
preserves a stable domain shape when multi-video and multi-music editing are
introduced later.
