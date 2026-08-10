# Preserve Planning Runs as immutable history

Every planning execution creates a new immutable Planning Run that snapshots
its Material References, request, configuration, model usage, and initial
Frozen Edit. Changing any planning input creates another run instead of
overwriting an earlier one, preserving reproducibility, comparison, and usage
attribution at the cost of retaining additional project history.
