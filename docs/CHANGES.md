# Changes

This file records user-visible changes to AI Image Studio. It is the release
history, not a roadmap; planned work belongs in `项目技术演进路线图.md`.

## How to maintain this file

- Add unreleased changes under `Unreleased` as part of the same pull request
  that implements them.
- Use the categories `Added`, `Changed`, `Fixed`, and `Security` where useful.
  Omit empty categories.
- Describe observable behavior, not implementation details that do not affect
  users.
- When a release is approved, move its entries to a version heading matching
  the Git tag (for example, `v8.0.0`) and add the release date in `YYYY-MM-DD`
  format. Create the tag only after the release commit is merged to `main`.
- Do not mark an open pull request as released.

## Unreleased

### Added

- Added a versioned data backup/restore utility for the complete Docker Compose
  data directory. Restore preserves the existing directory for rollback.

### Fixed

- RunningHub workflow overrides preserve the `ResolutionSelector` option label,
  match numeric values to workflow input types, and leave the random-seed
  sentinel unset so the platform generates a valid seed.
- RunningHub status polling retries up to three transient network failures
  without submitting the generation task again.

### Changed

- RunningHub image and video tasks use V2 combined status/result queries,
  adaptive polling, remote-task recovery, and bounded per-shot concurrency to
  reduce polling overhead and avoid duplicate submissions after worker restarts.

### Security

- Replaced custom API-key encryption with authenticated Fernet encryption.
  Storing new credentials now requires a high-entropy `ENCRYPTION_KEY`; legacy
  ciphertexts remain readable for migration, and the insecure Compose default
  was removed. Added an atomic command to rotate stored provider keys.

## v11.0.1 - 2026-11-05

### Fixed
- Corrected backend readiness labels so API health no longer implies ComfyUI is
  ready; ComfyUI status is shown only after a diagnostic check, and RunningHub
  is labeled as configured rather than connected.

## v11.0.0 - 2026-11-05

### Added

- Added an automatic video workspace for importing a single-episode ZIP with
  its storyboard and media assets. The selected LLM suggests shot-to-asset
  matches from storyboard text and filenames; users can review matches and
  generation settings before starting the job.
- Added whole-series folder import with one storyboard-and-assets subfolder per
  episode, natural episode ordering, review controls for every episode, and
  serial production that stops on failure so completed episodes are preserved
  and production can resume from the failed episode.
- Added folder drag-and-drop alongside the directory picker for whole-series
  imports.
- Added serial episode production through the MiniMax H3 Ref2VA ComfyUI
  workflow, including short-shot trimming, continuation segments for shots
  longer than 15 seconds, progress, cancellation, resume, and final MP4
  assembly with normalized dimensions and audio.
- Added persistent lifecycle events and classified errors for image/video
  generation tasks, with event-history, retry, and cancellation endpoints.
  Safe transient failures can be retried automatically up to twice or manually
  up to three times; tasks with a submitted ComfyUI prompt are resumed rather
  than submitted again. Cancellation targets only the matching queued/running
  prompt and reports when stopping it could not be confirmed.
- Added a standalone Director Agent workspace that turns one episode or script
  segment into an editable storyboard table and downloads it as an Excel
  workbook using the supplied production-sheet fields.
- Added reusable Director Agent skills that can be created in the UI or
  imported from a folder's `SKILL.md`, then selected for storyboard generation.
- Added automatic reference-asset planning from multi-episode storyboard
  workbooks, with background processing logs and per-episode asset generation.

### Changed

- Rebuilt the browser interface as a Vue 3 and TypeScript single-page
  application with Vite; the Agent workspace is now the default landing page,
  development requests use a same-origin API proxy, and FastAPI serves the
  compiled frontend from the Docker image.
- Replaced the GitHub Pages project showcase with a 36-second silent animated
  GIF embedded directly in the README. Removed the previous video and cover.

## v10.0.0 - 2026-11-03

### Changed

- The unauthenticated `/health` endpoint now reports API liveness only and no
  longer returns the internal ComfyUI API address; anonymous diagnostics
  provides the dependency checks.
- Removed the login/register UI, JWT endpoints, and bearer authentication for
  the single-instance edition. Business APIs are anonymous and must remain
  restricted to loopback or a trusted network.
- Reorganized the browser UI into **Custom operation** (open the configured
  ComfyUI web interface) and **Agent automation** (chat-driven image/video
  generation and ComfyUI control). Removed the duplicate in-app generation
  forms and ordinary chat mode; the underlying generation and chat APIs remain
  available for integrations.
- Agent automation now uses the selected built-in or custom LLM, sends recent
  browser-stored conversation history, and can optionally use RAG context.

### Added

- Added an optional visual quality Harness for MCP image/video generation.
  When enabled, the selected vision-capable model reviews generated images or
  five evenly sampled video frames; results below 8/10 receive up to two
  prompt-refinement retries. Review failures and exhausted retries are surfaced
  rather than treated as passing. Arbitrary ComfyUI workflow JSON is not
  rewritten or automatically retried.
- Added a selectable RunningHub generation backend for Agent automation.
  Users can securely save an API Key, discover text inputs in a custom workflow,
  map positive/negative prompt fields, and preview downloaded outputs in the
  existing result gallery. Visual-review retries submit new remote tasks and
  may incur additional RunningHub charges.
- Added an anonymous local deployment self-check for database, Redis,
  Celery worker, ComfyUI, media storage, and default LLM configuration. The
  check does not call the LLM or expose service URLs or API keys.
- Added conversational video generation through the ComfyUI MCP Agent and a
  configurable browser-facing ComfyUI URL. Users can save a browser-local,
  override and open the ComfyUI page from either UI section; Agent API control
  remains connected through the backend URL and does not require browser automation.
- Added architecture documentation and an engineering change/release record.

## v9.0.0 - 2026-10-28

### Added

- Added `POST /api/chat/stream` for server-sent event (SSE) chat output while
  retaining the JSON chat endpoint. Streaming supports configured LLM providers,
  RAG, and image input.

## v8.0.0

### Added

- Added instance-level personal generation preferences. The assistant can
  retrieve, update, and clear saved preferences and use them as generation
  defaults.

## v7.0.0

### Added

- Added MCP tools to inspect ComfyUI nodes, workflows, and input media.
- Added queue-item management and confirmation gates for impactful actions,
  including interruption, queue cleanup, model unloading, and media deletion.

## v6.0.0

### Added

- Added ComfyUI MCP control mode to the assistant, with tool progress and
  previews for generated media.

## v5.0.0

### Added

- Added a ComfyUI MCP server and an agent that can inspect ComfyUI, select
  available tools, and generate images.
- Added the initial agent-based generation flow and ComfyUI control tools.
