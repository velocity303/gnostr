# relays-and-settings-v1 — usability review (tux)

Reviewed: `docs/specs/relays-and-settings-v1.md` @ 8e3d8ad, 2026-10-22.
Verdict: **APPROVED WITH CHANGES.** The data model (A) and routing rules are
sound; the Settings restructure (B) and sidebar trim (C) need the fixes in the
final list before implementation cards start.
Grounding: GNOME/Adwaita HIG, libadwaita 1.x conventions, phosh/Librem 5
realities — 56px comfortable touch floor (44px hard minimum), 360×648 logical
viewport, no hover on touch so tooltips are not an affordance. The spec
already requires `AdwAlertDialog` (libadwaita ≥1.5), so 1.3/1.4-era widgets
(`AdwBanner`, `AdwSwitchRow`) are available.

## Q1 — Role editing at 360px: toggles vs switches vs popover

**Pick: neither inline chips nor a new popover — move the three role switches
into the per-relay `AdwExpanderRow` the spec already defines, and show the
current roles as the row subtitle** ("Read · Write", "Read only", "Write · DM").

- The proposed suffix budget does not fit: up, down, R, W, DM, remove = six
  suffix widgets. At 360px that leaves ~100px for the URL title; rows
  ellipsize to uselessness.
- One-letter R/W/DM labels are cryptic and untranslatable, and the proposed
  tooltips do not exist on touch (no hover on phosh).
- The spec rejects switches because "three rows per relay blow up row height"
  — inside a collapsed expander they cost zero height until opened, and each
  then gets a full-width row with a real label and a ≥48px touch target.
- The subtitle keeps role state glanceable without a tap; the expander is
  already the per-relay details surface (connection state, last error).
- Use `AdwSwitchRow` per role inside the expander. Remaining row suffixes:
  [↑] [↓] [remove] only, each ≥44px (56px preferred).

## Q2 — Relay Activity placement

**Pick: as proposed — bottom of the Relays page, not its own page.**

- It is relay-scoped diagnostics; a whole nav page for one log is heavy and
  dilutes the four-page split.
- Conditions: collapsed by default so "Add New Relay" stays reachable without
  scrolling past a log; monospace lines overflow 360px, so the log must live
  in its own horizontally-scrollable scrolled window and must never force the
  page itself to scroll sideways; cap retained lines (~200).

## Q3 — Logout placement and destructive-action distance

**Pick: as proposed — icon at the right end of the status row, single
`AdwAlertDialog` confirmation. One confirmation is sufficient** because this
logout is reversible (key stays in the keyring; re-login restores the
session). It is a session sign-out, not a key-destroying action.

- Do not bury it in a menu: that would be a second nav surface for one action,
  and the sidebar has no primary menu today.
- The dialog is the safety, so make it a real one: default focus on Cancel,
  "Log out" styled destructive (`destructive-suggested`). The spec's body copy
  ("you'll need your key or password to sign back in") is good — keep it.
- Affordance hygiene: tor/network icons are informational, logout is
  actionable. Separate the logout icon from the status glyphs with a
  separator or clear spacing, and make it a full 44–56px button, not a bare
  icon.

## Q4 — Page naming

**Pick: Account / Relays / Media / Network.**

- "General" is wrong for a page whose main content is key backup + profile
  sync — that is identity/account. "Account" also gives the key-health banner
  (Q5) an obvious target page name.
- "Media Upload" → "Media": shorter, still exact (the page is upload config).
- "Relays" and "Network" are right. Keep order: Account, Relays, Media,
  Network.

## Q5 — What the sidebar trim takes away

Real loss: **key-health visibility.** Today an unbacked-up or unhealthy key
warns in the sidebar on every launch; after the trim it lives two pages deep
and most users will never open it.

**Pick: require an `AdwBanner` on the main window** (libadwaita ≥1.3), shown
whenever key health is degraded (key present, backup never made or backup
failing): text "Your key isn't backed up", action "Back up" → opens Settings
→ Account. The banner is the standard HIG pattern for app-level warnings and
costs nothing when the key is healthy.

Nothing else from the trim is missed: the relay log is relay-scoped (Q2), Back
Up Key has a home on Account, and Logout gains the confirmation it should
always have had.

## Overall structure

The 4-page split is intuitive and matches how `AdwPreferencesWindow` collapses
to a page list + back button on phones — keep it. AC6 ("opens on last-used
page") needs explicit persistence of the selected page;
`AdwPreferencesWindow` does not do it for you.

## Changes required before implementation

1. Q1: role switches move into the per-relay expander; row subtitle shows the
   role summary; drop the R/W/DM toggle chips and tooltip reliance; row
   suffixes limited to up/down/remove at ≥44px.
2. Q5: add the key-health `AdwBanner` on the main window with a "Back up"
   action opening Settings → Account (belongs to the sidebar-trim card).
3. Q4: rename pages Account / Relays / Media / Network.
4. Q2: Relay Activity collapsed by default, own horizontal scroll region,
   ~200-line cap.
5. Q3: logout button ≥44px, visually separated from status icons; dialog
   defaults to Cancel, Log out styled destructive.
6. AC6: persist the last-used settings page in config.
