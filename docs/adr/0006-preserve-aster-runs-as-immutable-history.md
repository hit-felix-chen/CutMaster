# Preserve ASTER Runs as immutable history

**Status: Proposed — immutable ASTER Run history is not implemented.**

Once the project layer exists, every ASTER execution will create a new
immutable ASTER Run that snapshots its Material References, request,
configuration, model usage, and initial Frozen Edit. Changing any ASTER input
will create another run instead of overwriting an earlier one, preserving
reproducibility, comparison, and usage attribution at the cost of retaining
additional project history.

The current backend writes one set of Planners artifacts to the requested
output directory. With `overwrite=true`, it replaces those artifacts rather
than retaining an immutable run history.
