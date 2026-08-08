# Organize CutMaster as the MASTER Editing Team

CutMaster exposes one `Orchestrator` for complete runs plus independent
`Analyser`, `Planner`, and `Renderer` stage services. `ASTERTeam` coordinates
five role-bounded planning agents. Agent files live
at the roots of `analyser/` and `planners/`, while non-agent capabilities live
under their `tools/` directories; this makes the multi-agent research framing
match the executable architecture without misrepresenting deterministic
validation, scoring, and search as additional agents.
