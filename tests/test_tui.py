"""Tests for the TUI session tree viewer."""

import json
import os
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tui import SessionTreeApp, _build_trie, _sessions_dir, _encode_cwd


def _make_record(role, text, session_id, parent_uuid=None):
    """Create a minimal JSONL record."""
    uuid = str(uuid4())
    rec = {
        "type": "user" if role == "user" else "assistant",
        "uuid": uuid,
        "sessionId": session_id,
        "timestamp": "2026-03-14T00:00:00Z",
        "message": {
            "role": role,
            "content": text,
        },
    }
    if parent_uuid:
        rec["parentUuid"] = parent_uuid
    return rec


def _write_session(sessions_dir, cwd, session_id, records):
    """Write a list of records as a JSONL session file."""
    encoded = _encode_cwd(cwd)
    d = sessions_dir / encoded
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{session_id}.jsonl"
    with open(path, "w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return path


def _create_fork_sessions(sessions_dir, cwd):
    """Create two sessions sharing a prefix, then diverging.

    Session A: msg1, msg2, msg3, msg4, msg5_a
    Session B: msg1, msg2, msg3, msg4, msg5_b, msg6_b

    The fork point is after msg4.
    Returns (session_a_id, session_b_id).
    """
    sid_a = str(uuid4())
    sid_b = str(uuid4())

    # Shared prefix — same content, so they get the same content_hash
    shared = []
    for i in range(1, 5):
        role = "user" if i % 2 == 1 else "assistant"
        shared.append(_make_record(role, f"shared message {i}", sid_a))

    # Session A: shared + one unique message
    recs_a = [_make_record(r["type"], r["message"]["content"], sid_a) for r in shared]
    recs_a.append(_make_record("user", "session A unique message", sid_a))

    # Session B: shared + two unique messages
    recs_b = [_make_record(r["type"], r["message"]["content"], sid_b) for r in shared]
    recs_b.append(_make_record("user", "session B unique message", sid_b))
    recs_b.append(_make_record("assistant", "session B response", sid_b))

    _write_session(sessions_dir, cwd, sid_a, recs_a)
    _write_session(sessions_dir, cwd, sid_b, recs_b)

    return sid_a, sid_b


@pytest.fixture
def fork_env(tmp_path):
    """Set up a temp sessions dir with two forked sessions."""
    sessions_dir = tmp_path / "projects"
    sessions_dir.mkdir()
    cwd = "/test/project"

    sid_a, sid_b = _create_fork_sessions(sessions_dir, cwd)

    with patch("tui._sessions_dir", return_value=sessions_dir):
        yield {
            "cwd": cwd,
            "sessions_dir": sessions_dir,
            "sid_a": sid_a,
            "sid_b": sid_b,
        }


@pytest.mark.asyncio
async def test_select_session_only_expands_fork_points(fork_env):
    """After _select_session, only ancestors on the path to the target
    should be expanded — unrelated branches (e.g. session A) stay collapsed."""

    app = SessionTreeApp(fork_env["cwd"])
    sid_b = fork_env["sid_b"]

    with patch("tui._sessions_dir", return_value=fork_env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            # Wait for worker thread to finish loading
            await pilot.pause()
            for _ in range(20):
                await pilot.pause()
                if app.session_count > 0:
                    break

            tree = app.query_one("#tree")
            assert app.session_count > 0, "Sessions should be loaded"

            # Trigger select_session for session B
            app._select_session(sid_b)
            await pilot.pause()

            # Walk the tree and check what's expanded
            expanded_nodes = []
            collapsed_nodes = []

            def _collect(node, depth=0):
                if node is tree.root:
                    for child in node.children:
                        _collect(child, depth)
                    return
                data = app._get(node.data)
                if data:
                    info = {
                        "depth": depth,
                        "label": node.label.plain[:50],
                        "expanded": node.is_expanded,
                        "session_ids": data.get("session_ids", []),
                        "msg_count": data.get("msg_count", 1),
                    }
                    if node.is_expanded:
                        expanded_nodes.append(info)
                    else:
                        collapsed_nodes.append(info)
                for child in node.children:
                    _collect(child, depth + 1)

            _collect(tree.root)

            # Session B's unique branch should be reachable
            b_nodes = [n for n in expanded_nodes + collapsed_nodes
                       if fork_env["sid_b"] in n.get("session_ids", [])
                       and fork_env["sid_a"] not in n.get("session_ids", [])]
            assert len(b_nodes) > 0, "Session B unique nodes should exist"

            # Session A's unique branch should NOT be expanded — only the
            # target (session B) path should be opened.
            a_only_expanded = [n for n in expanded_nodes
                               if fork_env["sid_a"] in n["session_ids"]
                               and fork_env["sid_b"] not in n["session_ids"]]
            assert len(a_only_expanded) == 0, (
                f"Session A unique nodes should NOT be expanded: {a_only_expanded}"
            )

            # Only ancestors on the path to the target should be expanded,
            # so the total count should be small.
            assert len(expanded_nodes) <= 5, (
                f"Too many expanded nodes ({len(expanded_nodes)}), "
                f"expected <= 5: {expanded_nodes}"
            )


@pytest.mark.asyncio
async def test_select_session_target_is_visible(fork_env):
    """The selected node after _select_session should be the target session's
    deepest unique node."""

    app = SessionTreeApp(fork_env["cwd"])
    sid_b = fork_env["sid_b"]

    with patch("tui._sessions_dir", return_value=fork_env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            for _ in range(20):
                await pilot.pause()
                if app.session_count > 0:
                    break

            tree = app.query_one("#tree")
            assert app.session_count > 0, "Sessions should be loaded"

            app._select_session(sid_b)
            await pilot.pause()
            await pilot.pause()
            await pilot.pause()

            # The cursor should be on a node belonging to session B
            cursor = tree.cursor_node
            assert cursor is not None, "Cursor should be on a node"
            data = app._get(cursor.data)
            assert data is not None, "Cursor node should have data"
            assert sid_b in data.get("session_ids", []), (
                f"Cursor should be on session B, got {data.get('session_ids')}"
            )
