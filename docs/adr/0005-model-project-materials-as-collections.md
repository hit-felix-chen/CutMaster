# Model Edit Project Materials as extensible collections

**Status: Implemented in the Application, SQLite, and Project Setup Web UI.**

The Edit Project model and Application service represent video Materials and
music Materials as two collections of Material References. The current workflow
accepts exactly one video Material and one music Material in each request.
Keeping the project contract plural preserves a
stable domain shape when multi-video and multi-music editing are introduced
later.
