# Repository Guidelines

## Project Structure & Module Organization

This repository contains the AstrBot plugin `astrbot_plugin_whale_social`:

- `docs/astrbot_plugin_whale_social_PLAN.md` is the authoritative
  implementation plan and acceptance checklist. The `docs/` folder is
  git-ignored and kept local only; do not commit it.
- `docs/astrbot_plugin_whale_social_PLAN_v2.md` covers the conversation
  threads / debounce / group-level decision design.
- `docs/transcript.txt` preserves the source product discussion; treat it as
  reference material, not executable code.

When implementation begins, follow the layout defined in the plan:
`main.py` for plugin wiring and lifecycle hooks, `core/` for the collector,
gate, scoring, decision, reply, cooldown, and memory layers, `storage/` for
state persistence, and `data/state.json` for runtime state. Keep AstrBot
metadata and configuration at the plugin root (`metadata.yaml` and
`_conf_schema.json`).

## Build, Test, and Development Commands

There are no build scripts, dependencies, or automated tests in this initial
planning repository. Do not invent package-manager commands. Once Python code
is added, run focused tests with `pytest` (for example,
`pytest tests/test_gate.py`) and the full suite with `pytest`. Validate the
plugin manually in an AstrBot 4.5.7+ environment, starting with `dry_run` and
an explicit group allowlist.

## Coding Style & Naming Conventions

Use Python 3 with four-space indentation, type hints for public interfaces,
and `snake_case` for functions, variables, and module names. Use `PascalCase`
for classes and dataclasses, such as `GroupState` and `ChatMessage`. Keep
event handlers thin: put policy decisions in testable `core/` modules. Prefer
`asyncio`-safe code; protect shared state with locks and persist it via an
atomic temporary-file replacement.

## Testing Guidelines

Add `pytest` tests under `tests/`, named `test_<module>.py`; test functions
should describe the behavior, e.g. `test_human_message_resets_bot_streak`.
Cover cooldown persistence, response-window handling, rate limiting, JSON
decision fallbacks, duplicate events, and failed memory writeback. Tests must
not call an LLM or require a live AstrBot instance; mock those boundaries.

## Commit & Pull Request Guidelines

No Git history is present, so use concise imperative commit subjects, such as
`Add atomic group-state storage`. Keep commits narrowly scoped. Pull requests
should state the behavior change, link any relevant issue, list tests run, and
include configuration or log screenshots when changing WebUI settings or
observable decision behavior.

## Safety & Configuration

The plugin must remain observe-only for direct mentions: never stop event
propagation or send a duplicate reply. Keep the allowlist empty by default,
avoid persisting chat windows, and never commit credentials or real group data.
