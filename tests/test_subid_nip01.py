"""NIP-01 subscription-id length hygiene (HYGIENE card t_1c40a299).

NIP-01: a subscription id MUST be less than 64 characters. Relays accept an
over-long REQ id but reject the matching CLOSE — observed on FLX1 as
"bad close: invalid subscription id length" NOTICEs from relay.primal.net,
nostr.mom, nostr.bitcoiner.social, nos.lol — leaking the subscription until
the socket drops. The offender was fetch_thread's f"thread_{root_id}" (71
chars); every other producer was audited short (<= 29).

Contracts (real methods extracted via ast and bound to fake selves — house
pattern from test_relay_usage_economy.py):

- C1 nip01_sub_id: short ids pass through byte-identical; over-long ids clamp
  to <64 keeping the `prefix_` label; deterministic, idempotent, pinned
  sha256 vectors.
- C2 fetch_thread: REQs a legal (63-char) thread_ id, deterministic per root.
- C3 NostrRelay.subscribe: REQ carries the clamped id AND the later CLOSE
  carries the SAME clamped id (the leak: CLOSE of an id the relay never saw).
- C4 NostrRelay._enqueue: queued REQ ids are legal (EOSE auto-close sends
  CLOSE with the queued id — it must match what the relay accepted).
- C5 NostrClient.request_once: client-level maps keyed by sub_id see the
  same id the relay echoes back (backfill/dm-status fan-in).
- C6 static guard: every sub_id f-string construction/argument in src/
  estimates < 64 chars under worst-case inputs (64-hex ids, 13-digit ms).
"""

import ast
import hashlib
import json
import os
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src", "gnostr"))
_CLIENT_PATH = os.path.join(_SRC, "client.py")


def _extract(path, target, method_name, extra_ns=None):
    """Extract a real function from source (class method or module-level
    function) without importing the module under the mocked GObject."""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == target:
            fn = next(
                (
                    n
                    for n in node.body
                    if isinstance(n, ast.FunctionDef) and n.name == method_name
                ),
                None,
            )
            break
        if isinstance(node, ast.FunctionDef) and node.name == method_name:
            fn = node
            break
    if fn is None:
        raise LookupError(f"{target}.{method_name} not found in {path}")
    ns = {
        "json": json,
        "time": time,
        "hashlib": hashlib,
        "SUB_ID_MAX_LEN": 63,
        "REQ_DEDUP_WINDOW": 2.0,
    }
    if extra_ns:
        ns.update(extra_ns)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), path, "exec"), ns)
    return ns[method_name]


def _clamp():
    return _extract(_CLIENT_PATH, None, "nip01_sub_id")


# --- C1: the clamp function ------------------------------------------------


def test_short_ids_pass_through_unchanged():
    clamp = _clamp()
    for sid in [
        "dm_sub",
        "sub_following",
        "sub_global",
        "sub_me",
        "sub_zaps",
        "dm_poll_12",
        "dmstatus_3",
        "relays_deadbeef_1732000000",
        "sub_contacts_deadbeef",
        "meta_deadbeef",
        "extid_deadbeef",
        "badges_deadbeef",
        "blossom_deadbeef",
        "quote_deadbeef",
        "parent_deadbeef",
        "thread_replies_deadbeef",
        "nip46_deadbeef",
        "dmrelays_deadbeef",
        "fresh_following_1732000000",
        "backfill_1_1732000000000",
    ]:
        assert clamp(sid) == sid


def test_pinned_sha256_vectors():
    clamp = _clamp()
    # Pinned at authoring time: thread_<sha256("thread_"+eid)>[:56] body.
    assert (
        clamp("thread_" + "a" * 64)
        == "thread_e871597202552afbee1311d551f148d80238e7f0755368f4b3aaed11"
    )
    assert (
        clamp("thread_" + "0" * 64)
        == "thread_23195d82f65027282edb409f9d18e6699262f1a0277bada09e2a120b"
    )


def test_long_ids_always_under_64():
    clamp = _clamp()
    corpus = [
        "thread_" + "a" * 64,  # the observed offender (71)
        "sub_contacts_" + "b" * 64,
        "dm_" + "c" * 100,
        "x" * 64,  # no underscore at all
        "a" * 500,
        "thread_replies_" + "d" * 64,
    ]
    for sid in corpus:
        out = clamp(sid)
        assert len(out) < 64, f"{sid[:20]}... clamped to {len(out)}"


def test_clamp_is_idempotent_and_deterministic():
    clamp = _clamp()
    sid = "thread_" + "a" * 64
    once = clamp(sid)
    assert clamp(once) == once  # already-legal id passes through
    assert clamp(sid) == once  # pure function of the input


def test_prefix_preserved_and_injective():
    clamp = _clamp()
    ids = ["thread_" + format(i, "064x") for i in range(200)]
    clamped = [clamp(s) for s in ids]
    assert len(set(clamped)) == len(clamped)  # no collisions in a real corpus
    assert all(c.startswith("thread_") for c in clamped)
    # no-underscore input keeps no prefix: pure hex digest
    assert clamp("z" * 70) == hashlib.sha256(("z" * 70).encode()).hexdigest()[:63]


def test_boundary_63_passes_64_clamps():
    clamp = _clamp()
    assert clamp("p_" + "a" * 61) == "p_" + "a" * 61  # exactly 63
    assert len(clamp("p_" + "a" * 62)) == 63  # exactly 64 -> clamped


# --- C2: fetch_thread -------------------------------------------------------


class _FakeClientSelf:
    def __init__(self):
        self.subscribed = []

    def subscribe(self, sub_id, filters, snapshot=False):
        self.subscribed.append((sub_id, filters))


def test_fetch_thread_reqs_legal_thread_id():
    fetch_thread = _extract(
        _CLIENT_PATH, "NostrClient", "fetch_thread", {"nip01_sub_id": _clamp()}
    )
    self = _FakeClientSelf()
    root = "a" * 64
    fetch_thread(self, root)
    assert len(self.subscribed) == 1
    sub_id, filters = self.subscribed[0]
    assert sub_id.startswith("thread_")
    assert len(sub_id) < 64
    assert sub_id == "thread_e871597202552afbee1311d551f148d80238e7f0755368f4b3aaed11"
    # filters unchanged: root fetch + replies + reactions/reposts
    assert filters[0] == {"ids": [root]}
    assert filters[1]["#e"] == [root]

    # deterministic per root, distinct across roots
    self2 = _FakeClientSelf()
    fetch_thread(self2, root)
    assert self2.subscribed[0][0] == sub_id
    fetch_thread(self2, "b" * 64)
    assert self2.subscribed[1][0] != sub_id


# --- C3/C4: NostrRelay entry points -----------------------------------------


class _RecWS:
    def __init__(self):
        self.sent = []

    def send(self, data):
        self.sent.append(json.loads(data))


def _relay_self():
    import types

    return types.SimpleNamespace(
        is_connected=True,
        sub_id=None,
        ws=_RecWS(),
        active_sub_ids=set(),
        queued_sub_ids=set(),
        snapshot_ids=set(),
        live_subs={},
        _recent_reqs={},
        request_queue=[],
        _idle_closed=False,
        wake=lambda: None,
        process_queue=lambda: None,
    )


def test_relay_subscribe_clamps_req_and_close():
    sub = _extract(_CLIENT_PATH, "NostrRelay", "subscribe", {"nip01_sub_id": _clamp()})
    self = _relay_self()
    long_id = "thread_" + "a" * 64
    sub(self, long_id, {"kinds": [1]})
    req = self.ws.sent[0]
    assert req[0] == "REQ"
    clamped = req[1]
    assert len(clamped) < 64 and clamped.startswith("thread_")
    assert self.sub_id == clamped
    assert clamped in self.active_sub_ids and clamped in self.live_subs

    # The regression: opening a different sub CLOSEs the clamped id, never
    # the 71-char original the relay never recognized.
    sub(self, "sub_following", {"kinds": [1]})
    close = self.ws.sent[1]
    assert close == ["CLOSE", clamped]
    assert all(len(m[1]) < 64 for m in self.ws.sent)


def test_relay_enqueue_clamps_queued_id():
    enq = _extract(_CLIENT_PATH, "NostrRelay", "_enqueue", {"nip01_sub_id": _clamp()})
    self = _relay_self()
    enq(self, "thread_" + "b" * 64, {"ids": ["x"]})
    queued_id, _filters = self.request_queue[0]
    assert len(queued_id) < 64 and queued_id.startswith("thread_")
    assert queued_id in self.queued_sub_ids
    # dedup keys on the clamped id: a second enqueue is one REQ
    enq(self, "thread_" + "b" * 64, {"ids": ["x"]})
    assert len(self.request_queue) == 1


# --- C5: NostrClient.request_once --------------------------------------------


class _FakeRelay:
    def __init__(self):
        self.requested = []

    def request_once(self, sub_id, filters):
        self.requested.append(sub_id)


def test_client_request_once_clamps_before_fanout():
    ro = _extract(
        _CLIENT_PATH, "NostrClient", "request_once", {"nip01_sub_id": _clamp()}
    )
    import types

    relay = _FakeRelay()
    self = types.SimpleNamespace(_read_relays=lambda: {"wss://r": relay})
    ro(self, "thread_" + "c" * 64, {"kinds": [1]})
    assert len(relay.requested) == 1
    assert len(relay.requested[0]) < 64


# --- C6: static guard over every sub_id construction in src/ -----------------

_SUB_FUNCS = {"subscribe", "request_once", "_enqueue", "request_backfill"}


def _worst_case_len(joined):
    """Estimate the max length of a sub_id f-string: sliced values cost the
    slice width, timestamps 13 (epoch ms), seq counters 6, anything else a
    full 64-hex id."""
    total = 0
    for part in joined.values:
        if isinstance(part, ast.Constant):
            total += len(str(part.value))
            continue
        v = part.value if isinstance(part, ast.FormattedValue) else part
        if isinstance(v, ast.Subscript):
            sl = v.slice
            if isinstance(sl, ast.Constant):
                total += int(sl.value)
            elif isinstance(sl, ast.Slice) and isinstance(sl.upper, ast.Constant):
                total += int(sl.upper.value)
            else:
                total += 64
        elif isinstance(v, ast.Call):
            total += 13  # int(time.time() * 1000)
        elif isinstance(v, ast.Attribute) and "seq" in v.attr:
            total += 6
        elif isinstance(v, ast.Name) and "seq" in v.id:
            total += 6
        elif isinstance(v, ast.Name) and v.id == "kind":
            total += 2
        else:
            total += 64
    return total


def _is_clamped(node):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "nip01_sub_id"
    )


def test_all_sub_id_constructions_fit_nip01():
    offenders = []
    for root, _dirs, files in os.walk(_SRC):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(root, fname)
            tree = ast.parse(open(path, encoding="utf-8").read())
            for node in ast.walk(tree):
                targets = []
                if isinstance(node, ast.Call):
                    fn = node.func
                    name = (
                        fn.attr
                        if isinstance(fn, ast.Attribute)
                        else getattr(fn, "id", "")
                    )
                    if name in _SUB_FUNCS and node.args:
                        targets.append(node.args[0])
                elif (
                    isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.JoinedStr)
                    and any(
                        isinstance(t, ast.Name) and "sub_id" in t.id
                        for t in node.targets
                    )
                ):
                    targets.append(node.value)
                for arg in targets:
                    if isinstance(arg, ast.Constant):
                        est = len(arg.value)
                    elif _is_clamped(arg):
                        continue  # clamped by construction
                    elif isinstance(arg, ast.JoinedStr):
                        est = _worst_case_len(arg)
                    else:
                        continue  # dynamic; runtime clamp covers it
                    if est >= 64:
                        offenders.append(f"{path}:{node.lineno} est={est}")
    assert not offenders, "sub_id constructions >= 64 chars: " + "; ".join(offenders)


def test_entry_points_clamp_first():
    """Every wire-facing entry point clamps sub_id as its first statement,
    so no code path can key a set or send a frame with an illegal id."""
    src = open(_CLIENT_PATH, encoding="utf-8").read()
    tree = ast.parse(src)
    wanted = {
        ("NostrRelay", "subscribe"),
        ("NostrRelay", "_enqueue"),
        ("NostrClient", "subscribe"),
        ("NostrClient", "request_once"),
    }
    seen = set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for fn in node.body:
            if isinstance(fn, ast.FunctionDef) and (node.name, fn.name) in wanted:
                first = fn.body[0]
                if (
                    isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                ):
                    first = fn.body[1]  # skip docstring
                assert isinstance(first, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "sub_id" for t in first.targets
                ), f"{node.name}.{fn.name} must clamp sub_id first"
                call = first.value
                assert (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "nip01_sub_id"
                ), f"{node.name}.{fn.name} first stmt must call nip01_sub_id"
                seen.add((node.name, fn.name))
    assert seen == wanted, f"missing entry points: {wanted - seen}"
