---
name: Spec-driven work
about: Issue anchored to a spec document and diagram (for spec implementation work)
title: ''
labels: ''
ref: ''
---

## Spec anchor
<!-- Link the exact spec doc + anchor (or commit/issue holding the JSON contract) -->
- Spec doc:
- Anchor / section:

## Diagram reference
<!-- Link or path to the diagram this work implements against -->
- Diagram:

## Scope
- One-line description of what this component implements.

## Target platforms
- Select: gnome / phosh / plasma-mobile / all
-

## Portal & D-Bus contracts
- List any portal (org.freedesktop.portal.*) or D-Bus service contracts introduced/consumed.

## Layout & breakpoints
- Any layout / breakpoint requirements from the spec.

## Memory-safety & platform notes
- Notable constraints (Wayland-only inputs, memory-safety requirements, etc.).

## Acceptance criteria
- [ ] Implementation matches the spec anchor linked above
- [ ] Diagram reference above is current with the implementation
- [ ] Tests / checks against this spec pass (list below)
- [ ] DOX pass done (nearest AGENTS.md updated if contracts changed)

## Verification
- Tests / checks the implementer ran against this spec:
