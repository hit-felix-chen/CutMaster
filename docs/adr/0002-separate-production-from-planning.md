# Keep Production separate from ASTER planning tools

Music profiling is a request-specific tool owned through the Arrangement
Architect, so it lives under `planners/tools/` and is reached through
`ASTERTeam`. Post-planning script adaptation, audio preparation, and rendering
remain a separate `production/` package because they realize an approved edit
rather than make planning decisions; placing them under ASTER tools would blur
the agent boundary and make `CutMaster` depend on a team's private tools.
