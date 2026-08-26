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

The coordinator resolves its Edit Project through the Application's atomic
get-or-create-by-name command. Repeating the same CLI or Benchmark Project Name
therefore preserves one Web-visible Project and appends a new immutable ASTER
Run. Interactive Web creation and rename remain strict: an occupied Project
Name is a conflict whose response identifies the existing Project for direct
navigation.

Mashup-Benchmark receives a versioned managed execution receipt whose artifact
paths are relative to the Application Data Root. After successful completion,
the Benchmark adapter validates every reference against that root and copies
only the evaluation artifacts into its own `runs/<run_id>/task_outputs/`
directory. Those copies are submission artifacts; the CutMaster Project and
its managed history remain authoritative.

The Application owns logging for this synchronous complete execution. Every
Material Analysis, ASTER Planning, or Rendering Job that actually runs writes
its canonical `<data_root>/logs/jobs/<job_id>.log`, just as a
Supervisor-launched Web Worker does. The complete synchronous execution also
publishes an ordered canonical `workflow.log`; its receipt exposes that path,
the conditional `analyser.video_job_log` and `analyser.music_job_log` paths,
and the required `planners.job_log` and `renderer.job_log` paths. Web tails the
per-Job file for real-time Attempt inspection. Mashup-Benchmark does not
redirect or capture CutMaster process output to build `logs/backend.log`;
after success it validates the receipt reference and copies the canonical
Workflow Log unchanged into that evaluation-facing path.
