"""Create-account wizard logic — the gi mocks make the imported class
unusable for instantiation (same problem as followers_list_view), so we
ast-extract the real methods from onboarding.py and bind them to a FakeView.
"""

import ast
import textwrap
from unittest.mock import MagicMock

from gnostr.ui.onboarding import gate_passed

SRC = "src/gnostr/ui/onboarding.py"


def _extract(method_names):
    tree = ast.parse(open(SRC).read())
    cls = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "CreateAccountWindow"
    )
    out = {}
    for name in method_names:
        fn = next(
            f for f in cls.body if isinstance(f, ast.FunctionDef) and f.name == name
        )
        ns = {"gate_passed": gate_passed}
        exec(compile(ast.Module([fn], []), SRC, "exec"), ns)
        out[name] = ns[name]
    return out


METHODS = _extract(["_on_gate_confirmed"])


class FakeView:
    def __init__(self):
        self.view_stack = MagicMock()
        self.client = MagicMock()
        self.main_window = MagicMock()
        self._nsec = "nsec1abc"
        self._gate_entry = MagicMock()
        self._gate_error = MagicMock()

    _on_gate_confirmed = METHODS["_on_gate_confirmed"]


def test_gate_requires_exact_nsec():
    assert gate_passed("nsec1abc", " nsec1abc ") is True
    assert gate_passed("nsec1abc", "nsec1abd") is False


def test_gate_confirm_advances_stack():
    v = FakeView()
    v._gate_entry.get_text.return_value = "nsec1abc"
    v._on_gate_confirmed(None)
    v.view_stack.set_visible_child_name.assert_called_with("wrap")


def test_gate_mismatch_does_not_advance():
    v = FakeView()
    v._gate_entry.get_text.return_value = "wrong"
    v._on_gate_confirmed(None)
    v.view_stack.set_visible_child_name.assert_not_called()
    v._gate_error.set_text.assert_called_once()
