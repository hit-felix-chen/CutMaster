# Use unique Project Names and reuse managed workspaces

**Status: Accepted and implemented.**

Edit Projects need both an opaque storage identity and a predictable
user-facing workspace identity. Allowing duplicate Project Names made repeated
Mashup-Benchmark executions appear as separate cards even though the caller's
Benchmark Run ID and Task ID produced the same stable name.

Project Names are now globally unique after the existing whitespace
normalization. The SQLite schema is the concurrency authority. Interactive
create and rename commands reject an occupied name and return the existing
Project ID in Problem Details. The Web create dialog presents that collision
as an explicit choice to edit the proposed name or navigate to the existing
Project.

Complete managed workflows use an atomic get-or-create-by-name Application
command. CLI and Mashup-Benchmark therefore reuse an existing same-name
Project and create a new ASTER Run beneath it. They do not list Projects and
race a later create, and the frontend does not merge unrelated records at
render time.

The opaque Project ID remains authoritative for relationships and managed
artifact paths. Project Name remains editable, but a rename may only choose an
available name and never moves Project, Run, Frozen Edit, or Render Variant
artifacts.
