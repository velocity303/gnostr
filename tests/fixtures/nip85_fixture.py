#!/usr/bin/env python3
"""NIP-85 Trust Shield test fixtures (spec t_d4ed8091).

Deterministic: fixture scalars are sha256 of fixed strings; timestamps fixed.
Uses gnostr's own signer (nostr_utils) so vectors verify in gnostr's own
verification path. Output: one JSON object per line (fixture file format).
"""
import hashlib
import json
import sys

import importlib.util as _ilu  # load nostr_utils standalone (gnostr/__init__ pulls gi)
_spec = _ilu.spec_from_file_location("nostr_utils", "/home/james/projects/gnostr/src/gnostr/nostr_utils.py")
nu = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(nu)

T0 = 1763500000  # fixed created_at base for all fixture events


def fixture_sk(name: str) -> str:
    sk = hashlib.sha256(f"trust-shield-fixture:{name}".encode()).hexdigest()
    return sk


def ev(sk, kind, tags, content, created_at):
    pk = nu.get_public_key(sk)
    e = {
        "pubkey": pk,
        "created_at": created_at,
        "kind": kind,
        "tags": tags,
        "content": content,
    }
    return nu.sign_event(e, sk)


provider_sk = fixture_sk("provider")
provider_pk = nu.get_public_key(provider_sk)
targets = {}
for n in range(1, 8):
    sk = fixture_sk(f"target{n}")
    targets[f"target{n}"] = nu.get_public_key(sk)

out = []

# provider identity (NIP-85 appendix 1: kind 0 per service key)
out.append(
    ev(
        provider_sk,
        0,
        [],
        json.dumps(
            {
                "name": "Trust Shield Fixture Provider",
                "about": "Deterministic NIP-85 test provider. Do not use in production.",
                "website": "http://127.0.0.1:8080",
            },
            separators=(",", ":"),
        ),
        T0,
    )
)

# ranks: target1=90 (trusted), target2=75, target3=25 (boundary, keep),
# target4=24 (collapse), target5=10 (collapse), target6 no rank event,
# target7 rank=10 but a FOLLOWED target (exemption case).
ranks = {"target1": 90, "target2": 75, "target3": 25, "target4": 24, "target5": 10, "target7": 10}
for name, rank in sorted(ranks.items()):
    out.append(
        ev(
            provider_sk,
            30382,
            [["d", targets[name]], ["rank", str(rank)]],
            "",
            T0 + 1,
        )
    )

# malformed example: rank out of range (must be dropped by the verifier)
out.append(
    ev(
        provider_sk,
        30382,
        [["d", targets["target6"]], ["rank", "400"]],
        "",
        T0 + 2,
    )
)

print(f"provider_pubkey={provider_pk}")
for k, v in sorted(targets.items()):
    print(f"{k}_pubkey={v}")
for e in out:
    # sanity: id recomputes
    assert nu.compute_event_id(e) == e["id"]
    print(json.dumps(e, separators=(",", ":")))
