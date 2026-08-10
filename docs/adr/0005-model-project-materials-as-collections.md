# Model Edit Project Materials as extensible collections

An Edit Project models its video Materials and music Materials as two
collections of Material References even though the initial product accepts
exactly one member in each collection to match the current Planner. Keeping the
contract plural from the outset preserves a stable project and API shape when
multi-video and multi-music editing are introduced later.
