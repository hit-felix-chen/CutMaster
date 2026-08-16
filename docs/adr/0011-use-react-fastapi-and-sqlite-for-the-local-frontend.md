# Use React, FastAPI, and SQLite for the local frontend

**Status: React, FastAPI, SQLite persistence, SPA packaging, Project Setup, and
managed ASTER planning implemented; SSE and other managed long-job execution
proposed.**

The local CutMaster application uses a React and TypeScript SPA built with Vite
and a FastAPI Web adapter over the implemented,
framework-independent Python Application Layer. SQLite already stores project
and job metadata while media and artifacts remain on the filesystem. REST plus
Server-Sent Events is sufficient because the browser
sends discrete commands and receives one-way progress updates; Next.js, Gradio,
Streamlit, binary database storage, and cloud-oriented infrastructure would add
weight without serving the first-release local single-user boundary.

REST uses resource-oriented `GET` queries and explicit use-case command routes,
not database-shaped CRUD or a universal command endpoint. Proposed SSE is read-only;
FastAPI performs transport mapping while Application services own all rules.
HTTP failures use RFC Problem Details with stable domain codes and structured
metadata so the React client can localize errors without exposing internal
exceptions or encoding domain decisions in route handlers.
HTTP URLs use one owner level for child collections and flat canonical IDs for
entity details and commands, avoiding deep persistence-shaped route trees.
React Router owns navigable state, TanStack Query owns server state, and local
React state owns unsaved forms and revisions; small contexts cover only locale,
Colour Mode, and the global SSE connection, so the first release needs no Redux
or Zustand store that duplicates backend resources. Authored React/Vite source
lives in the repository-level `web/` project, while FastAPI remains under
`src/cutmaster/adapters/web/`; ignored `web/dist/` assets are mapped into that
adapter's package resources only while building release artifacts. Release
sdists include the prebuilt SPA so a wheel built from the sdist does not require
Node.js; installed wheels serve those resources through `importlib.resources`.
Editable frontend development uses the Vite development server and FastAPI
proxy rather than treating generated assets as authored Python source.
