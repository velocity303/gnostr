# DOX framework

- DOX is a self-documenting AGENTS.md hierarchy installed here
- Agent must follow DOX instructions across any edits
- AGENTS.md files are binding work contracts for their subtrees

## Core Contract

- Work products, source materials, instructions, records, assets, and durable docs must stay understandable from the nearest applicable AGENTS.md plus every parent AGENTS.md above it
- The DOX tree is the single source of truth for project architecture, conventions, and boundaries
- AI agents (Hermes, OpenCode, Claude Code) MUST walk the DOX tree before editing any file

## Read Before Editing

1. Read this root AGENTS.md
2. Identify every file or folder you expect to touch
3. Walk from the repository root to each target path
4. Read every AGENTS.md found along each route
5. Use the nearest AGENTS.md as the local contract and parent docs for repo-wide rules
6. If docs conflict, the closer doc controls local work details, but no child doc may weaken DOX

Do not rely on memory. Re-read the applicable DOX chain before editing.

## Update After Editing

Every meaningful change requires a DOX pass before the task is done.

Update the closest owning AGENTS.md when a change affects:
- purpose, scope, ownership, or responsibilities
- durable structure, contracts, workflows, or operating rules
- required inputs, outputs, permissions, constraints, side effects, or artifacts
- user preferences about behavior, communication, process, organization, or quality
- AGENTS.md creation, deletion, move, rename, or index contents

Update parent docs when parent-level structure, ownership, workflow, or child index changes.

## Project Overview

**Gnostr** — native Linux Nostr client built for GNOME Desktop. Written in Python with GTK4/libadwaita, built with Meson/Flatpak.

Architecture:
- `src/gnostr/main.py` — application entry point, MainWindow, GnostrApp
- `src/gnostr/client.py` — Nostr relay WebSocket client
- `src/gnostr/renderer.py` — content renderer (text, images, video, nostr cards)
- `src/gnostr/database.py` — SQLite event/profile storage
- `src/gnostr/dialogs.py` — login, compose, relay preference dialogs
- `src/gnostr/key_manager.py` — nsec key storage via libsecret
- `src/gnostr/nostr_utils.py` — Nostr protocol utilities (bech32, keys)
- `src/gnostr/connection_status.py` — relay connection status tracking
- `src/gnostr/gateway/` — relay connection gateway
- `src/gnostr/service/` — feed/profile metadata services
- `src/gnostr/ui/` — GTK widgets (feed view, post widget, profile view, thread view, sidebar)
- `src/gnostr/util/` — cache manager, connection state
- `tests/` — pytest test suite with xvfb for GTK rendering
- `data/` — desktop file, icons, GSettings schema, metainfo
- `po/` — translations

Build system: Meson with Flatpak Builder. CI via Gitea Actions (`.gitea/workflows/ci.yaml`).

## Work Guidance

- All Python code follows `pyproject.toml` conventions: black format, isort, flake8 (max-line-length=120)
- GTK code uses Gtk4 + Adw (libadwaita-1). Do not use Gtk3 patterns
- Tests use pytest with xvfb-run for headless GTK rendering
- Mock `gi.repository` modules in tests — never import real GTK/GStreamer in test suite
- Video playback uses `playbin3` GStreamer pipeline, not `uridecodebin`
- All images loaded async via ImageLoader thread pool (max 16 workers)
- Nostr protocol: connect via WebSockets to relays, subscribe with filters, receive events
- Private keys stored in system keyring via libsecret, never in plaintext files
- Prefer `Adw.NavigationSplitView` for desktop layout, responsive breakpoint at 800px

## Verification

- `pytest tests/` — all tests pass under xvfb
- `black src/ tests/` — formatting check
- `isort src/ tests/` — import ordering
- `flake8 src/ tests/ --max-line-length=120` — lint
- `flatpak-builder --force-clean --repo=repo build-dir tech.livingonlinux.gnostr.json` — Flatpak build
- CI pipeline runs lint → test → build on push to main

## Closeout

1. Re-check changed paths against the DOX chain
2. Update nearest owning docs and any affected parents or children
3. Refresh every affected Child DOX Index
4. Remove stale or contradictory text
5. Run existing verification when relevant
6. Report any docs intentionally left unchanged and why

## Child DOX Index

| Path | Scope |
|------|-------|
| `src/gnostr/AGENTS.md` | Core app: MainWindow, GnostrApp, app lifecycle, signal wiring |
| `src/gnostr/gateway/AGENTS.md` | Relay connection, WebSocket gateway |
| `src/gnostr/service/AGENTS.md` | Feed service, profile service, metadata service |
| `src/gnostr/ui/AGENTS.md` | GTK widgets: feed view, post widget, profile view, thread view, sidebar |
| `src/gnostr/util/AGENTS.md` | Cache manager, connection state utilities |
| `tests/AGENTS.md` | Testing conventions, mocking patterns, CI setup |
