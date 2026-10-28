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

- V9 adds `POST /api/chat/stream` for server-sent event (SSE) chat output while
  retaining the JSON chat endpoint. Streaming supports configured LLM providers,
  RAG, and image input. This work is in [PR #4](https://github.com/Cheung-Fly/ai-image-studio-Base-On-ComfyUI/pull/4)
  and is not yet a release.
- Added architecture documentation and an engineering change/release record.

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
