# Store CLI and Benchmark executions as managed history

**Status: Implemented.**

CLI and Mashup-Benchmark complete executions create the same managed Material,
Edit Project, ASTER Run, Execution Attempt, Frozen Edit, and Render Variant
records as the local Web workspace. They do not accept a caller-selected output
directory for canonical artifacts and do not create a separate non-project
workflow bundle.
All canonical media and planning artifacts therefore remain under the active
Application Data Root with portable managed references and are immediately
visible in the Web UI.

CLI and Benchmark call the Application managed-workflow coordinator, which
claims and runs the same durable Analyser, Planners, and Renderer Jobs used by
Web and the worker adapter. Adapter-only Planners controls are
stored in a separate immutable Run planning-options snapshot so Benchmark
semantics survive Retry and Resume without contaminating Effective
Configuration.

Mashup-Benchmark receives a versioned managed execution receipt whose artifact
paths are relative to the Application Data Root. After successful completion,
the Benchmark adapter validates every reference against that root and copies
only the evaluation artifacts into its own `runs/<run_id>/task_outputs/`
directory. Those copies are submission artifacts; the CutMaster Project and
its managed history remain authoritative.
