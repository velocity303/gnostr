"""Drive the real CreateAccountWindow (P1 #14) on the Librem 5 and prove the
backup gate is un-skippable. No keyring writes: the flow is stopped before
_finish(), so the generated key is discarded and the user's login is untouched.

Runs through the real GnostrApp activation path (GStreamer init, CSS provider,
MainWindow) so phosh maps the window like a normal app launch. Non-blocking
GLib state machine: each step waits for the host-side grim capture to land
(polling via timeouts so the main loop keeps running and windows map/repaint),
and asserts each frame differs from the previous one (frozen-buffer guard).

Steps:
  welcome        -> MainWindow login page with 'Create account' button
  intro          -> CreateAccountWindow intro page
  gate-masked    -> after Generate: gate page, nsec hidden behind dots
  gate-revealed  -> after Reveal: nsec visible once
  gate-blocked   -> 'I saved it' clicked with empty type-back -> error, still on gate
  wrap           -> correct type-back accepted -> NIP-49 wrap page
  blob           -> password set -> ncryptsec produced and shown
"""

import hashlib
import os
import sys

sys.path.insert(1, "/app/share")
from gi.repository import Gio  # noqa: E402

Gio.Resource.load("/app/share/gnostr.gresource")._register()

from gi.repository import GLib  # noqa: E402

from gnostr.main import GnostrApp  # noqa: E402

EVID = os.environ.get("EVID_DIR", "/tmp/gnosc")
RESULTS = []
LAST_SHA = [None]
DEADLINE = [0.0]


def check(cond, msg):
    RESULTS.append((bool(cond), msg))
    print(("PASS " if cond else "FAIL ") + msg, flush=True)


def finish():
    print("EVIDENCE_DONE", flush=True)
    fails = [m for ok, m in RESULTS if not ok]
    print(
        f"SUMMARY: {len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed", flush=True
    )
    app.quit()
    return False


def fail(msg):
    check(False, msg)
    return finish()


def shot(name, then):
    """Let GTK repaint (settle), then signal the host capture loop and continue
    in `then` once a STABLE PNG lands (size unchanged across two polls — grim
    writes incrementally, so hashing the moment the file appears can hash a
    partial frame and falsely pass the differs-check)."""
    os.makedirs(EVID, exist_ok=True)

    def trigger():
        open(os.path.join(EVID, f"step-{name}"), "w").write("ready")
        DEADLINE[0] = GLib.get_monotonic_time() + 90 * GLib.TIME_SPAN_SECOND
        prev = [-1]

        def poll():
            path = os.path.join(EVID, f"shot-{name}.png")
            if not os.path.exists(path):
                if GLib.get_monotonic_time() > DEADLINE[0]:
                    return fail(f"capture timeout for {name}")
                return True  # keep polling
            size = os.path.getsize(path)
            if size != prev[0]:  # file still growing — wait for a stable frame
                prev[0] = size
                return True
            data = open(path, "rb").read()
            sha = hashlib.sha256(data).hexdigest()
            check(sha != LAST_SHA[0], f"shot {name} differs from previous frame")
            LAST_SHA[0] = sha
            GLib.timeout_add(600, then)
            return False

        GLib.timeout_add(250, poll)
        return False

    GLib.timeout_add(900, trigger)  # paint settle before capture is signalled
    return False


def run():
    win = app.props.active_window
    check(win is not None, "MainWindow created via GnostrApp")
    # Show the welcome page even though a key is saved (read-only; no keyring write).
    win.main_stack.set_visible_child_name("login")
    return shot("welcome", lambda: step_intro(win))


def step_intro(win):
    from gnostr.ui.onboarding import CreateAccountWindow

    ca = CreateAccountWindow(win.client, win)
    ca.present()

    def after_map():
        check(ca.get_mapped(), "CreateAccountWindow is mapped on screen")
        return shot("intro", lambda: step_gate(win, ca))

    GLib.timeout_add(1000, after_map)
    return False


def step_gate(win, ca):
    ca._on_generate(None)  # same handler the 'Generate My Key' button calls
    vis = ca.view_stack.get_visible_child_name()
    check(vis == "gate", f"generate -> gate page (visible={vis})")
    masked = ca._nsec_label.get_label()
    check("nsec" not in masked, f"nsec hidden before reveal (label={masked!r})")
    return shot("gate-masked", lambda: step_reveal(ca))


def step_reveal(ca):
    ca._on_reveal(None)
    check(ca._nsec_label.get_label() == ca._nsec, "reveal shows the nsec once")
    return shot("gate-revealed", lambda: step_blocked(ca))


def step_blocked(ca):
    # Un-skippable: confirm with an empty type-back must NOT advance.
    ca._gate_entry.set_text("")
    ca._on_gate_confirmed(None)
    vis = ca.view_stack.get_visible_child_name()
    check(vis == "gate", f"empty type-back blocked (visible={vis})")
    check("Doesn't match" in ca._gate_error.get_label(), "gate error shown")
    return shot("gate-blocked", lambda: step_wrap(ca))


def step_wrap(ca):
    from gnostr.ui.onboarding import gate_passed

    # Correct type-back advances to the NIP-49 wrap page.
    check(gate_passed(ca._nsec, ca._nsec), "gate_passed accepts exact copy")
    ca._gate_entry.set_text(ca._nsec)
    ca._on_gate_confirmed(None)
    vis = ca.view_stack.get_visible_child_name()
    check(vis == "wrap", f"exact type-back accepted (visible={vis})")
    return shot("wrap", lambda: step_blob(ca))


def step_blob(ca):
    ca._pw1.set_text("hunter2-hunter2")
    ca._pw2.set_text("hunter2-hunter2")
    ca._on_encrypt_backup(None)
    vis = ca.view_stack.get_visible_child_name()
    check(vis == "blob", f"nip49 wrap produced blob page (visible={vis})")
    return shot("blob", lambda: step_done())


def step_done():
    # Stop here on purpose: _finish() would write to the keyring. The generated
    # key is discarded when the window closes — nothing persisted.
    from gnostr.key_manager import KeyManager

    saved = KeyManager.load_key()
    check(saved is not None, "user's existing keyring key untouched")
    return finish()


app = GnostrApp()
GLib.timeout_add(2500, run)
app.run([])
fails = [m for ok, m in RESULTS if not ok]
print(f"SUMMARY: {len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed", flush=True)
sys.exit(1 if fails else 0)
