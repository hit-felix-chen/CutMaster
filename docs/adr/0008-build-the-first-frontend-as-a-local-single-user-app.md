# Build the first frontend as a local single-user application

**Status: Implemented for the current local Web workspace.**

The first CutMaster frontend runs with its backend on the same machine and
serves one local user without authentication or tenant isolation. This keeps
long-form footage, the Material Library, analysis caches, FFmpeg, and durable
background jobs close to the existing filesystem-based workflow; a hosted
multi-user service is deferred because it would first require object storage,
large-file upload infrastructure, account isolation, quotas, and distributed
job execution.
