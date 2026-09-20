# Contributing to Gnostr

## Dev environment (SOP — every contributor and every agent box)

Don't install into system Python. One command bootstraps an identical venv on
any machine, including PEP 668 "externally-managed" distros (Fedora/Nobara etc.)
where bare `pip install` fails:

```bash
uv venv .venv && uv pip install -r requirements-dev.txt   # uv preferred
# fallback without uv:
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

`requirements-dev.txt` is the single source of truth for the toolchain
(pytest, black, isort, flake8 + runtime deps). If you add a dev dependency,
add it there in the same PR — CI and every other box stay in sync through it.

## Build & Run

Gnostr is built with Flatpak Builder:

```bash
flatpak-builder --force-clean --repo=repo build_dir tech.livingonlinux.gnostr.json
flatpak build-bundle repo gnostr.flatpak tech.livingonlinux.gnostr
```

## Conventions

- Python 3.11+, GTK4 + libadwaita-1 only — no GTK3 patterns.
- Formatting: `black` + `isort`; lint gate: `flake8 --max-line-length=120`. CI runs these; keep the tree clean so failures are always *new* failures.
- Tests mock `gi.repository` — the suite must run headless (`xvfb-run pytest tests/`) and never import real GTK/GStreamer.
- Video playback uses `playbin3` + `gtk4paintablesink` (not `uridecodebin`, not appsink).
- New `src/gnostr/**/*.py` modules MUST be added to the `install_data` list in the nearest `meson.build` — the Flatpak only ships what meson installs. `tests/test_meson_install_contract.py` enforces this; dev checkouts work without it, Flatpaks crash at import.
- Private keys go through `key_manager` (libsecret) — never plaintext.

## Verifying changes

```bash
xvfb-run pytest tests/
black --check src/ tests/
flake8 src/ tests/ --max-line-length=120
```

## DOX

This repo uses an AGENTS.md (DOX) hierarchy as its documentation contract.
Before editing, read the root `AGENTS.md` and every `AGENTS.md` along the path
to the files you touch. After meaningful changes, update the nearest owning
`AGENTS.md` (and parents if structure/ownership changed). `TRACKING.md` holds
the current bug/feature workstream state — add an entry when you start
multi-commit work, resolve it when verified.
