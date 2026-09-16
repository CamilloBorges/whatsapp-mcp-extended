"""Store path segments must be collision-free.

The previous scheme replaced every unsafe character with "_", which is not
injective: two distinct identities could map to the same directory and would
then share one WhatsApp account.
"""

import base64
import importlib.util
import os

_SPEC = importlib.util.spec_from_file_location(
    "run_server",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run_server.py"),
)
run_server = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(run_server)

_path_segment = run_server._path_segment


def test_ids_differing_only_in_punctuation_do_not_collide():
    # The exact pair the old sanitiser collapsed together.
    assert _path_segment("adam@x.com") != _path_segment("adam.x.com")


def test_hex_ids_are_left_alone():
    """Existing store directories must keep their names — no migration."""
    user_id = "77f00f25ead04b3b90c67898431154dd"
    assert _path_segment(user_id) == user_id


def test_safe_ids_pass_through_unchanged():
    for value in ("simple", "with-dash", "with_underscore", "MiXed123"):
        assert _path_segment(value) == value


def test_unsafe_ids_are_encoded_and_reversible():
    encoded = _path_segment("adam@x.com")
    assert encoded.startswith("b~")
    padded = encoded[2:] + "=" * (-len(encoded[2:]) % 4)
    assert base64.urlsafe_b64decode(padded).decode() == "adam@x.com"


def test_a_separator_cannot_survive_encoding():
    """A "/" must never reach the path as a separator."""
    assert "/" not in _path_segment("a/b")
    assert _path_segment("a/b") != _path_segment("a_b")


def test_encoded_and_passthrough_namespaces_are_disjoint():
    """A raw id must not be able to impersonate an encoded one."""
    # "~" is not in the safe set, so a passthrough value can never start with
    # the prefix — meaning this literal gets encoded rather than passed through.
    assert _path_segment("b~YWRhbUB4LmNvbQ") != "b~YWRhbUB4LmNvbQ"


def test_distinct_inputs_stay_distinct():
    values = [
        "77f00f25ead04b3b90c67898431154dd",
        "adam@x.com",
        "adam.x.com",
        "adam_x_com",
        "a/b",
        "a_b",
        "b~x",
        "",
        "default",
    ]
    segments = [_path_segment(v) for v in values]
    assert len(set(segments)) == len(values)


def test_empty_id_still_produces_a_segment():
    # Must not return "", which would collapse into the parent directory.
    assert _path_segment("") not in ("", ".", "..")
