# Organize CutMaster as the MASTER Editing Team

**Status: Implemented; original entry-point context retained below.**

**Amendment:** [ADR 0016](0016-treat-cli-web-and-benchmark-as-peer-adapters.md)
supersedes this ADR's original permanent complete-workflow facade. The
MASTER/ASTER roles and stage ownership below remain implemented; CLI and
Benchmark now use the Application Layer directly, and that facade has been
removed.

At the time of this decision, CutMaster exposed one complete-workflow facade
plus independent
`Analyser`, `Planners`, and `Renderer` stage services. `ASTERTeam` coordinates
five role-bounded editorial agents. Agent files now live
at the roots of `workflow/analyser/` and `workflow/planners/`, while non-agent capabilities live
under their `tools/` directories; this makes the multi-agent research framing
match the executable architecture without misrepresenting deterministic
validation, scoring, and search as additional agents.
