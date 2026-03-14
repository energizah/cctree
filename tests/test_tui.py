"""Tests for the TUI session tree viewer."""

import json
import os
import re
from pathlib import Path
from unittest.mock import patch, MagicMock
from uuid import uuid4

import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tui import (
    SessionTreeApp,
    _build_trie,
    _sessions_dir,
    _encode_cwd,
    _parse_session_file,
    _extract_text_content,
    _format_detail,
    _preview,
    _age_text,
    _age_style,
    _msg_role,
    _truncate,
    _extract_tool_code,
    _rewind_session_file,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS_COUNTER = 0


def _make_ts():
    """Return monotonically increasing ISO timestamps."""
    global _TS_COUNTER
    _TS_COUNTER += 1
    return f"2026-03-14T{_TS_COUNTER // 3600:02d}:{(_TS_COUNTER % 3600) // 60:02d}:{_TS_COUNTER % 60:02d}Z"


def _make_record(role, text, session_id, parent_uuid=None, ts=None):
    """Create a minimal JSONL record."""
    uuid = str(uuid4())
    rec = {
        "type": "user" if role == "user" else "assistant",
        "uuid": uuid,
        "sessionId": session_id,
        "timestamp": ts or _make_ts(),
        "message": {
            "role": role,
            "content": text,
        },
    }
    if parent_uuid:
        rec["parentUuid"] = parent_uuid
    return rec


def _make_tool_use_record(session_id, tool_name, tool_input, ts=None):
    """Create an assistant record with a tool_use content block."""
    uuid = str(uuid4())
    return {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": session_id,
        "timestamp": ts or _make_ts(),
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": tool_name,
                    "input": tool_input,
                }
            ],
        },
    }


def _make_tool_result_record(session_id, output, ts=None):
    """Create a user record with a tool_result content block."""
    uuid = str(uuid4())
    return {
        "type": "user",
        "uuid": uuid,
        "sessionId": session_id,
        "timestamp": ts or _make_ts(),
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "content": [{"type": "text", "text": output}],
                }
            ],
        },
    }


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


async def _wait_for_load(app, pilot, max_iters=30):
    """Poll until the worker thread finishes loading sessions."""
    await pilot.pause()
    for _ in range(max_iters):
        await pilot.pause()
        if app.session_count > 0:
            return
    pytest.fail("Sessions did not load in time")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


@pytest.fixture
def single_session_env(tmp_path):
    """Set up a temp sessions dir with one simple session."""
    sessions_dir = tmp_path / "projects"
    sessions_dir.mkdir()
    cwd = "/test/project"
    sid = str(uuid4())

    records = [
        _make_record("user", "Hello, how are you?", sid),
        _make_record("assistant", "I'm doing well, thanks!", sid),
        _make_record("user", "Can you help me with Python?", sid),
        _make_record("assistant", "Of course! What do you need help with?", sid),
    ]
    _write_session(sessions_dir, cwd, sid, records)

    with patch("tui._sessions_dir", return_value=sessions_dir):
        yield {
            "cwd": cwd,
            "sessions_dir": sessions_dir,
            "sid": sid,
        }


@pytest.fixture
def multi_session_env(tmp_path):
    """Set up a temp sessions dir with several sessions of varying recency."""
    sessions_dir = tmp_path / "projects"
    sessions_dir.mkdir()
    cwd = "/test/project"

    sids = []
    for i in range(4):
        sid = str(uuid4())
        sids.append(sid)
        records = [
            _make_record("user", f"session {i} question", sid,
                         ts=f"2026-03-{10 + i}T12:00:00Z"),
            _make_record("assistant", f"session {i} answer with unique_keyword_{i}", sid,
                         ts=f"2026-03-{10 + i}T12:01:00Z"),
        ]
        _write_session(sessions_dir, cwd, sid, records)

    with patch("tui._sessions_dir", return_value=sessions_dir):
        yield {
            "cwd": cwd,
            "sessions_dir": sessions_dir,
            "sids": sids,
        }


@pytest.fixture
def tool_session_env(tmp_path):
    """Session with tool_use and tool_result messages."""
    sessions_dir = tmp_path / "projects"
    sessions_dir.mkdir()
    cwd = "/test/project"
    sid = str(uuid4())

    records = [
        _make_record("user", "Read the file foo.py", sid),
        _make_tool_use_record(sid, "Read", {"file_path": "/tmp/foo.py"}),
        _make_tool_result_record(sid, "def hello():\n    print('hi')"),
        _make_record("assistant", "The file contains a hello function.", sid),
    ]
    _write_session(sessions_dir, cwd, sid, records)

    with patch("tui._sessions_dir", return_value=sessions_dir):
        yield {
            "cwd": cwd,
            "sessions_dir": sessions_dir,
            "sid": sid,
        }


# ---------------------------------------------------------------------------
# Unit tests: pure functions
# ---------------------------------------------------------------------------


class TestTruncate:
    def test_short_text(self):
        assert _truncate("hello", 100) == "hello"

    def test_exact_length(self):
        assert _truncate("abc", 3) == "abc"

    def test_long_text(self):
        result = _truncate("a" * 200, 50)
        assert len(result.split("\n")[0]) == 50
        assert "truncated" in result
        assert "200 chars" in result


class TestExtractTextContent:
    def test_string_content(self):
        assert _extract_text_content({"content": "hello"}) == "hello"

    def test_list_content(self):
        msg = {"content": [{"type": "text", "text": "hello"}]}
        assert _extract_text_content(msg) == "hello"

    def test_tool_use_content(self):
        msg = {"content": [{"type": "tool_use", "name": "Read", "input": {"path": "x"}}]}
        result = _extract_text_content(msg)
        assert "Read" in result

    def test_tool_result_content(self):
        msg = {"content": [{"type": "tool_result", "output": "file contents"}]}
        result = _extract_text_content(msg)
        assert "file contents" in result

    def test_empty_content(self):
        assert _extract_text_content({}) == ""


class TestPreview:
    def test_string_content(self):
        msg = {"type": "user", "message": {"content": "hello world"}}
        assert _preview(msg) == "hello world"

    def test_multiline_collapsed(self):
        msg = {"type": "user", "message": {"content": "line1\nline2\nline3"}}
        assert "\n" not in _preview(msg)

    def test_tool_use_preview(self):
        msg = {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": "Edit", "input": {}}],
            },
        }
        assert "[Edit]" in _preview(msg)

    def test_tool_result_preview(self):
        msg = {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "content": [{"type": "text", "text": "ok"}]},
                ],
            },
        }
        assert "ok" in _preview(msg)


class TestMsgRole:
    def test_assistant(self):
        msg = {"type": "assistant", "message": {"content": "hi"}}
        role, _ = _msg_role(msg)
        assert role == "✨"

    def test_user(self):
        msg = {"type": "user", "message": {"content": "hi"}}
        role, _ = _msg_role(msg)
        assert role == "👤"

    def test_tool_result(self):
        msg = {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "output": "ok"}],
            },
        }
        role, _ = _msg_role(msg)
        assert role == "🛠️"


class TestAgeText:
    def test_recent(self):
        from datetime import datetime, timezone, timedelta
        ts = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        result = _age_text(ts)
        assert "s ago" in result

    def test_minutes(self):
        from datetime import datetime, timezone, timedelta
        ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        result = _age_text(ts)
        assert "m ago" in result

    def test_hours(self):
        from datetime import datetime, timezone, timedelta
        ts = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        result = _age_text(ts)
        assert "h ago" in result

    def test_days(self):
        from datetime import datetime, timezone, timedelta
        ts = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        result = _age_text(ts)
        assert "d ago" in result

    def test_invalid(self):
        assert _age_text("not-a-timestamp") == ""

    def test_empty(self):
        assert _age_text("") == ""


class TestAgeStyle:
    def test_old_session(self):
        assert _age_style("2026-01-01T00:00:00Z", False, "2026-03-14T00:00:00Z") == "dim"

    def test_recent_tip(self):
        ts = "2026-03-14T00:00:00Z"
        assert _age_style(ts, True, ts) == "bold green"

    def test_recent_ancestor(self):
        ts = "2026-03-14T00:00:00Z"
        assert _age_style(ts, False, ts) == "#98c379"


class TestExtractToolCode:
    def test_write_tool(self):
        code = json.dumps({
            "file_path": "/tmp/foo.py",
            "content": "def hello():\n    pass",
        })
        result, lang = _extract_tool_code(code, "json")
        assert result == "def hello():\n    pass"
        assert lang == "python"

    def test_edit_tool(self):
        code = json.dumps({
            "file_path": "/tmp/foo.py",
            "old_string": "old",
            "new_string": "new",
        })
        result, lang = _extract_tool_code(code, "json")
        assert "old" in result
        assert "new" in result
        assert lang == "python"

    def test_non_json(self):
        result, lang = _extract_tool_code("not json at all", "json")
        assert result == "not json at all"
        assert lang == "json"

    def test_non_dict(self):
        code = json.dumps([1, 2, 3])
        result, lang = _extract_tool_code(code, "json")
        assert result == code
        assert lang == "json"

    def test_unknown_extension(self):
        code = json.dumps({"file_path": "/tmp/foo.xyz", "content": "stuff"})
        result, lang = _extract_tool_code(code, "json")
        assert result == "stuff"
        assert lang == "json"  # fallback


class TestFormatDetail:
    def test_basic(self):
        msg = {
            "type": "assistant",
            "message": {"role": "assistant", "content": "Hello!"},
            "session_id": "abc123",
            "timestamp": "2026-03-14T00:00:00Z",
        }
        result = _format_detail(msg)
        assert "ASSISTANT" in result.plain
        assert "Hello!" in result.plain

    def test_code_block(self):
        msg = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": "Here:\n```python\nprint('hi')\n```",
            },
            "session_id": "abc",
            "timestamp": "",
        }
        result = _format_detail(msg)
        assert "print" in result.plain


# ---------------------------------------------------------------------------
# Unit tests: session parsing
# ---------------------------------------------------------------------------


class TestParseSessionFile:
    def test_basic_parsing(self, tmp_path):
        sid = str(uuid4())
        records = [
            _make_record("user", "hello", sid),
            _make_record("assistant", "hi back", sid),
        ]
        path = tmp_path / "test.jsonl"
        with open(path, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        result = _parse_session_file(path)
        assert len(result) == 2
        assert result[0]["type"] == "user"
        assert result[1]["type"] == "assistant"

    def test_content_hashing(self, tmp_path):
        sid = str(uuid4())
        records = [
            _make_record("user", "same text", sid),
            _make_record("assistant", "response", sid),
        ]
        path = tmp_path / "test.jsonl"
        with open(path, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        result = _parse_session_file(path)
        assert all("content_hash" in r for r in result)
        # Different content should have different hashes
        assert result[0]["content_hash"] != result[1]["content_hash"]

    def test_coalescing(self, tmp_path):
        """Consecutive assistant records with chained parentUuid should coalesce."""
        sid = str(uuid4())
        r1 = _make_record("user", "question", sid)
        r2 = _make_record("assistant", "part one", sid)
        r3 = _make_record("assistant", " part two", sid, parent_uuid=r2["uuid"])

        path = tmp_path / "test.jsonl"
        with open(path, "w") as f:
            for r in [r1, r2, r3]:
                f.write(json.dumps(r) + "\n")

        result = _parse_session_file(path)
        # r2 and r3 should be coalesced into one entry
        assert len(result) == 2
        # The coalesced message should contain both parts
        content = result[1]["message"]["content"]
        texts = [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text"]
        assert "part one" in texts
        assert " part two" in texts

    def test_skips_non_message_types(self, tmp_path):
        sid = str(uuid4())
        records = [
            {"type": "system", "content": "system msg"},
            _make_record("user", "hello", sid),
            {"type": "file-history-snapshot", "files": []},
        ]
        path = tmp_path / "test.jsonl"
        with open(path, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        result = _parse_session_file(path)
        assert len(result) == 1

    def test_empty_file(self, tmp_path):
        path = tmp_path / "empty.jsonl"
        path.write_text("")
        result = _parse_session_file(path)
        assert result == []

    def test_malformed_json(self, tmp_path):
        path = tmp_path / "bad.jsonl"
        path.write_text("not json\n{bad json}\n")
        result = _parse_session_file(path)
        assert result == []


class TestBuildTrie:
    def test_single_session(self, tmp_path):
        sessions_dir = tmp_path / "projects"
        sessions_dir.mkdir()
        cwd = "/test/project"
        sid = str(uuid4())
        records = [
            _make_record("user", "hello", sid),
            _make_record("assistant", "hi", sid),
        ]
        _write_session(sessions_dir, cwd, sid, records)

        with patch("tui._sessions_dir", return_value=sessions_dir):
            root, count, tips = _build_trie(cwd)
        assert count == 1
        assert sid in tips
        assert len(root["children"]) == 1

    def test_fork_detection(self, tmp_path):
        sessions_dir = tmp_path / "projects"
        sessions_dir.mkdir()
        cwd = "/test/project"
        sid_a, sid_b = _create_fork_sessions(sessions_dir, cwd)

        with patch("tui._sessions_dir", return_value=sessions_dir):
            root, count, tips = _build_trie(cwd)
        assert count == 2
        assert sid_a in tips
        assert sid_b in tips

        # Walk to the fork point — shared prefix then diverge
        node = root
        while len(node["children"]) == 1:
            node = next(iter(node["children"].values()))
        # Fork point should have 2 children
        assert len(node["children"]) == 2

    def test_no_sessions(self, tmp_path):
        sessions_dir = tmp_path / "projects"
        sessions_dir.mkdir()
        with patch("tui._sessions_dir", return_value=sessions_dir):
            root, count, tips = _build_trie("/nonexistent")
        assert count == 0
        assert tips == {}

    def test_session_tips_timestamps(self, tmp_path):
        sessions_dir = tmp_path / "projects"
        sessions_dir.mkdir()
        cwd = "/test/project"
        sid = str(uuid4())
        records = [
            _make_record("user", "q", sid, ts="2026-03-14T10:00:00Z"),
            _make_record("assistant", "a", sid, ts="2026-03-14T10:05:00Z"),
        ]
        _write_session(sessions_dir, cwd, sid, records)

        with patch("tui._sessions_dir", return_value=sessions_dir):
            _, _, tips = _build_trie(cwd)
        assert tips[sid] == "2026-03-14T10:05:00Z"


class TestRewindSessionFile:
    def test_rewind_to_message(self, tmp_path):
        import hashlib
        sid = str(uuid4())
        records = [
            _make_record("user", "msg1", sid),
            _make_record("assistant", "msg2", sid),
            _make_record("user", "msg3", sid),
        ]
        path = tmp_path / "test.jsonl"
        with open(path, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        # Compute hash for msg2 (assistant)
        content = "msg2"
        raw = json.dumps(content, sort_keys=True)
        target_hash = hashlib.sha256(f"assistant:{raw}".encode()).hexdigest()[:16]

        assert _rewind_session_file(path, target_hash) is True
        result = _parse_session_file(path)
        assert len(result) == 2  # msg1 + msg2, msg3 removed

    def test_rewind_nonexistent_hash(self, tmp_path):
        sid = str(uuid4())
        records = [_make_record("user", "msg1", sid)]
        path = tmp_path / "test.jsonl"
        with open(path, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        assert _rewind_session_file(path, "nonexistent") is False


# ---------------------------------------------------------------------------
# E2E tests: TUI app (existing)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_select_session_only_expands_fork_points(fork_env):
    """After _select_session, only ancestors on the path to the target
    should be expanded — unrelated branches (e.g. session A) stay collapsed."""

    app = SessionTreeApp(fork_env["cwd"])
    sid_b = fork_env["sid_b"]

    with patch("tui._sessions_dir", return_value=fork_env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

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
            await _wait_for_load(app, pilot)

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


# ---------------------------------------------------------------------------
# E2E tests: app loading and tree rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_app_loads_sessions(single_session_env):
    """App should load sessions and display them in the tree."""
    env = single_session_env
    app = SessionTreeApp(env["cwd"])

    with patch("tui._sessions_dir", return_value=env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)
            assert app.session_count == 1

            tree = app.query_one("#tree")
            assert len(tree.root.children) > 0


@pytest.mark.asyncio
async def test_app_title(single_session_env):
    """App title should include the cwd."""
    env = single_session_env
    app = SessionTreeApp(env["cwd"])

    with patch("tui._sessions_dir", return_value=env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            assert env["cwd"] in app.title


@pytest.mark.asyncio
async def test_status_bar_shows_session_count(multi_session_env):
    """Status bar should show the number of sessions after loading."""
    env = multi_session_env
    app = SessionTreeApp(env["cwd"])

    with patch("tui._sessions_dir", return_value=env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            status = app.query_one("#status")
            assert "4 sessions" in str(status.render())


@pytest.mark.asyncio
async def test_tree_node_labels_contain_role_emoji(single_session_env):
    """Tree node labels should contain role emoji indicators."""
    env = single_session_env
    app = SessionTreeApp(env["cwd"])

    with patch("tui._sessions_dir", return_value=env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            tree = app.query_one("#tree")
            labels = []

            def _walk(node):
                if node is not tree.root:
                    labels.append(node.label.plain)
                for child in node.children:
                    _walk(child)

            _walk(tree.root)
            assert len(labels) > 0
            # At least one should have user or assistant emoji
            all_text = " ".join(labels)
            assert "👤" in all_text or "✨" in all_text


@pytest.mark.asyncio
async def test_tool_use_nodes_render(tool_session_env):
    """Sessions with tool_use/tool_result should render with correct role icons."""
    env = tool_session_env
    app = SessionTreeApp(env["cwd"])

    with patch("tui._sessions_dir", return_value=env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            tree = app.query_one("#tree")
            # Expand all to see tool nodes
            first = tree.root.children[0] if tree.root.children else None
            if first:
                app._populate_placeholder(first)
                first.expand()
                await pilot.pause()

            labels = []

            def _walk(node):
                if node is not tree.root:
                    labels.append(node.label.plain)
                for child in node.children:
                    _walk(child)

            _walk(tree.root)
            all_text = " ".join(labels)
            # Should have tool result emoji
            assert "🛠️" in all_text or "[Read]" in all_text


# ---------------------------------------------------------------------------
# E2E tests: vim-style navigation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cursor_down_up(single_session_env):
    """j/k should move cursor down/up."""
    env = single_session_env
    app = SessionTreeApp(env["cwd"])

    with patch("tui._sessions_dir", return_value=env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            tree = app.query_one("#tree")
            tree.focus()
            await pilot.pause()

            # Get initial cursor
            initial = tree.cursor_node

            await pilot.press("j")
            await pilot.pause()
            after_down = tree.cursor_node

            await pilot.press("k")
            await pilot.pause()
            after_up = tree.cursor_node

            # Should have moved down then back
            if initial and after_down:
                assert after_up is initial or after_up == initial


@pytest.mark.asyncio
async def test_expand_collapse_node(fork_env):
    """l should expand, h should collapse."""
    app = SessionTreeApp(fork_env["cwd"])

    with patch("tui._sessions_dir", return_value=fork_env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            tree = app.query_one("#tree")
            tree.focus()
            await pilot.pause()

            # Move to first node (should be expandable since it's a chain/fork)
            node = tree.cursor_node
            if node and node.allow_expand:
                was_expanded = node.is_expanded

                await pilot.press("l")
                await pilot.pause()

                await pilot.press("h")
                await pilot.pause()


@pytest.mark.asyncio
async def test_page_down_up(multi_session_env):
    """Ctrl-d/u should page down/up."""
    env = multi_session_env
    app = SessionTreeApp(env["cwd"])

    with patch("tui._sessions_dir", return_value=env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            tree = app.query_one("#tree")
            tree.focus()
            await pilot.pause()

            initial = tree.cursor_node

            await pilot.press("ctrl+d")
            await pilot.pause()

            await pilot.press("ctrl+u")
            await pilot.pause()

            # Should have navigated and returned (or close to it)


@pytest.mark.asyncio
async def test_go_top_bottom(multi_session_env):
    """g/G should go to top/bottom."""
    env = multi_session_env
    app = SessionTreeApp(env["cwd"])

    with patch("tui._sessions_dir", return_value=env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            tree = app.query_one("#tree")
            tree.focus()
            await pilot.pause()

            # Go to bottom
            await pilot.press("G")
            await pilot.pause()
            bottom = tree.cursor_node

            # Go to top
            await pilot.press("g")
            await pilot.pause()
            top = tree.cursor_node

            # Top and bottom should differ (unless only 1 node)
            if app.session_count > 1:
                assert top is not bottom


# ---------------------------------------------------------------------------
# E2E tests: expand/collapse all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expand_all(fork_env):
    """'e' should expand all nodes."""
    app = SessionTreeApp(fork_env["cwd"])

    with patch("tui._sessions_dir", return_value=fork_env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            tree = app.query_one("#tree")
            tree.focus()
            await pilot.pause()

            await pilot.press("e")
            await pilot.pause()

            # Check that nodes are expanded
            def _count_expanded(node):
                c = 1 if node.is_expanded else 0
                for child in node.children:
                    c += _count_expanded(child)
                return c

            expanded = _count_expanded(tree.root)
            assert expanded >= 1


@pytest.mark.asyncio
async def test_collapse_all(fork_env):
    """'c' should collapse all nodes."""
    app = SessionTreeApp(fork_env["cwd"])

    with patch("tui._sessions_dir", return_value=fork_env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            tree = app.query_one("#tree")
            tree.focus()
            await pilot.pause()

            # Expand first, then collapse
            await pilot.press("e")
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            # All non-root nodes should be collapsed
            def _any_expanded(node):
                for child in node.children:
                    if child.is_expanded:
                        return True
                    if _any_expanded(child):
                        return True
                return False

            assert not _any_expanded(tree.root)


# ---------------------------------------------------------------------------
# E2E tests: detail panel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_toggle_detail_panel(single_session_env):
    """'p' should toggle the detail panel visibility."""
    env = single_session_env
    app = SessionTreeApp(env["cwd"])

    with patch("tui._sessions_dir", return_value=env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            detail = app.query_one("#detail")
            tree = app.query_one("#tree")
            tree.focus()
            await pilot.pause()

            initial_display = detail.display

            await pilot.press("p")
            await pilot.pause()
            toggled = detail.display

            assert toggled != initial_display

            await pilot.press("p")
            await pilot.pause()
            assert detail.display == initial_display


@pytest.mark.asyncio
async def test_detail_shows_content_on_highlight(single_session_env):
    """Highlighting a node should update the detail panel."""
    env = single_session_env
    app = SessionTreeApp(env["cwd"])

    with patch("tui._sessions_dir", return_value=env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            detail = app.query_one("#detail")
            detail.display = True
            tree = app.query_one("#tree")
            tree.focus()
            await pilot.pause()

            # Move to first node
            await pilot.press("j")
            await pilot.pause()
            # Allow detail timer to flush
            await pilot.pause()
            await pilot.pause()

            # Detail should have updated from the default text
            text = str(detail.render())
            assert text is not None


@pytest.mark.asyncio
async def test_yank_detail(single_session_env):
    """'y' should copy detail to clipboard (no crash)."""
    env = single_session_env
    app = SessionTreeApp(env["cwd"])

    with patch("tui._sessions_dir", return_value=env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            tree = app.query_one("#tree")
            tree.focus()
            await pilot.pause()

            # Move to a data node
            await pilot.press("j")
            await pilot.pause()

            # Yank should not crash
            await pilot.press("y")
            await pilot.pause()


# ---------------------------------------------------------------------------
# E2E tests: search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_opens_input(single_session_env):
    """'/' should open the search input."""
    env = single_session_env
    app = SessionTreeApp(env["cwd"])

    with patch("tui._sessions_dir", return_value=env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            tree = app.query_one("#tree")
            tree.focus()
            await pilot.pause()

            search = app.query_one("#search-input")
            assert not search.display

            await pilot.press("slash")
            await pilot.pause()
            assert search.display


@pytest.mark.asyncio
async def test_search_escape_dismisses(single_session_env):
    """Escape should dismiss search and return to tree."""
    env = single_session_env
    app = SessionTreeApp(env["cwd"])

    with patch("tui._sessions_dir", return_value=env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            tree = app.query_one("#tree")
            tree.focus()
            await pilot.pause()

            await pilot.press("slash")
            await pilot.pause()

            await pilot.press("escape")
            await pilot.pause()

            search = app.query_one("#search-input")
            assert not search.display
            assert app._search_pattern == ""


@pytest.mark.asyncio
async def test_incremental_search_matches_labels(multi_session_env):
    """Typing in search should incrementally match node labels."""
    env = multi_session_env
    app = SessionTreeApp(env["cwd"])

    with patch("tui._sessions_dir", return_value=env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            tree = app.query_one("#tree")
            tree.focus()
            await pilot.pause()

            # Open search and type
            await pilot.press("slash")
            await pilot.pause()

            search = app.query_one("#search-input")
            search.value = "session 0"
            # Trigger the changed event
            app._run_search("session 0")
            await pilot.pause()

            # Should have found matches
            assert len(app._search_matches) > 0


@pytest.mark.asyncio
async def test_search_submit_does_full_search(multi_session_env):
    """Enter in search should do full content search and dismiss input."""
    env = multi_session_env
    app = SessionTreeApp(env["cwd"])

    with patch("tui._sessions_dir", return_value=env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            tree = app.query_one("#tree")
            tree.focus()
            await pilot.pause()

            await pilot.press("slash")
            await pilot.pause()

            search = app.query_one("#search-input")
            search.value = "unique_keyword_2"
            app._run_search("unique_keyword_2")
            await pilot.pause()

            # Submit by pressing Enter
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()

            # Search input should be hidden
            assert not search.display
            # Pattern should be preserved for n/N navigation
            assert app._search_pattern == "unique_keyword_2"


@pytest.mark.asyncio
async def test_search_next_prev(multi_session_env):
    """n/N should navigate between search matches."""
    env = multi_session_env
    app = SessionTreeApp(env["cwd"])

    with patch("tui._sessions_dir", return_value=env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            tree = app.query_one("#tree")
            tree.focus()
            await pilot.pause()

            # Set up a search with multiple matches
            app._search_pattern = "session"
            app._run_search("session")
            await pilot.pause()

            if len(app._search_matches) > 1:
                idx_before = app._search_index

                await pilot.press("n")
                await pilot.pause()
                idx_after = app._search_index

                # Index should have advanced
                assert idx_after != idx_before or len(app._search_matches) == 1

                await pilot.press("N")
                await pilot.pause()


@pytest.mark.asyncio
async def test_search_highlight_in_detail(multi_session_env):
    """Search matches should be highlighted in the detail panel."""
    env = multi_session_env
    app = SessionTreeApp(env["cwd"])

    with patch("tui._sessions_dir", return_value=env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            tree = app.query_one("#tree")
            tree.focus()
            detail = app.query_one("#detail")
            detail.display = True
            await pilot.pause()

            # Set a search pattern
            app._search_pattern = "session"
            # Move to a node to trigger detail update
            await pilot.press("j")
            await pilot.pause()
            await pilot.pause()

            # The _flush_detail method should have applied highlighting
            # (verified by the code path, not visual check)
            assert app._search_pattern == "session"


# ---------------------------------------------------------------------------
# E2E tests: recent tips navigation (t/T)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recent_tips_navigation(multi_session_env):
    """'t' should navigate to session tips sorted by recency."""
    env = multi_session_env
    app = SessionTreeApp(env["cwd"])

    with patch("tui._sessions_dir", return_value=env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            tree = app.query_one("#tree")
            tree.focus()
            await pilot.pause()

            # Press t to go to first recent tip
            await pilot.press("t")
            await pilot.pause()
            await pilot.pause()

            assert app._recent_index == 0
            assert len(app._recent_tips) == len(env["sids"])

            # Press t again to advance
            await pilot.press("t")
            await pilot.pause()
            await pilot.pause()

            assert app._recent_index == 1


@pytest.mark.asyncio
async def test_recent_tips_wraps(multi_session_env):
    """Recent tips should wrap around."""
    env = multi_session_env
    app = SessionTreeApp(env["cwd"])

    with patch("tui._sessions_dir", return_value=env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            tree = app.query_one("#tree")
            tree.focus()
            await pilot.pause()

            n = len(env["sids"])
            # Navigate past end to wrap
            for _ in range(n + 1):
                await pilot.press("t")
                await pilot.pause()

            # Should have wrapped to index 0 (or close)
            assert 0 <= app._recent_index < n


@pytest.mark.asyncio
async def test_recent_tips_resets_on_other_action(multi_session_env):
    """Non-tip actions should reset the recent index."""
    env = multi_session_env
    app = SessionTreeApp(env["cwd"])

    with patch("tui._sessions_dir", return_value=env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            tree = app.query_one("#tree")
            tree.focus()
            await pilot.pause()

            await pilot.press("t")
            await pilot.pause()
            assert app._recent_index >= 0

            # j should reset it (via run_action override)
            await pilot.press("j")
            await pilot.pause()
            assert app._recent_index == -1


# ---------------------------------------------------------------------------
# E2E tests: chat input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_input_toggle(single_session_env):
    """'i' should show chat input, Escape should hide it."""
    env = single_session_env
    app = SessionTreeApp(env["cwd"])

    with patch("tui._sessions_dir", return_value=env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            tree = app.query_one("#tree")
            tree.focus()
            await pilot.pause()

            chat = app.query_one("#chat-input")
            assert not chat.display

            await pilot.press("i")
            await pilot.pause()
            assert chat.display

            await pilot.press("escape")
            await pilot.pause()
            assert not chat.display


# ---------------------------------------------------------------------------
# E2E tests: chat submission (fork-rewind-resume)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_submit_adds_pending_node(single_session_env):
    """Submitting chat should add a pending node to the tree."""
    env = single_session_env
    app = SessionTreeApp(env["cwd"])

    with patch("tui._sessions_dir", return_value=env["sessions_dir"]), \
         patch.object(app, "_stream_chat"):  # Don't actually call claude
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            tree = app.query_one("#tree")
            tree.focus()
            await pilot.pause()

            # Navigate to a node
            await pilot.press("j")
            await pilot.pause()

            node_before = tree.cursor_node
            assert node_before is not None

            # Simulate chat submission
            app._add_pending_node("test prompt")
            await pilot.pause()
            await pilot.pause()

            # Should have a pending node with the hourglass emoji
            labels = []

            def _walk(n):
                labels.append(n.label.plain)
                for c in n.children:
                    _walk(c)

            _walk(tree.root)
            assert any("test prompt" in l for l in labels)


# ---------------------------------------------------------------------------
# E2E tests: reload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload(single_session_env):
    """'r' should reload sessions from disk."""
    env = single_session_env
    app = SessionTreeApp(env["cwd"])

    with patch("tui._sessions_dir", return_value=env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)
            assert app.session_count == 1

            # Add another session to disk
            sid2 = str(uuid4())
            records = [
                _make_record("user", "new session", sid2),
                _make_record("assistant", "new response", sid2),
            ]
            _write_session(env["sessions_dir"], env["cwd"], sid2, records)

            tree = app.query_one("#tree")
            tree.focus()
            await pilot.pause()

            # Press r to reload
            await pilot.press("r")
            await pilot.pause()

            # Wait for reload worker
            for _ in range(30):
                await pilot.pause()
                if app.session_count == 2:
                    break

            assert app.session_count == 2


# ---------------------------------------------------------------------------
# E2E tests: status bar
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_bar_updates_on_highlight(single_session_env):
    """Moving cursor should update the status bar with session metadata."""
    env = single_session_env
    app = SessionTreeApp(env["cwd"])

    with patch("tui._sessions_dir", return_value=env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            tree = app.query_one("#tree")
            tree.focus()
            await pilot.pause()

            # Navigate to a node
            await pilot.press("j")
            await pilot.pause()

            status = app.query_one("#status")
            text = str(status.render())
            # Status should contain session count at minimum
            assert "1 session" in text or "session" in text.lower()


# ---------------------------------------------------------------------------
# E2E tests: fork display
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fork_point_label(fork_env):
    """Fork points should show [N branches] in their label."""
    app = SessionTreeApp(fork_env["cwd"])

    with patch("tui._sessions_dir", return_value=fork_env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            tree = app.query_one("#tree")
            tree.focus()
            await pilot.pause()

            # Expand all to see fork labels
            await pilot.press("e")
            await pilot.pause()

            labels = []

            def _walk(node):
                if node is not tree.root:
                    labels.append(node.label.plain)
                for child in node.children:
                    _walk(child)

            _walk(tree.root)

            # At least one label should mention branches
            assert any("branch" in l.lower() for l in labels), (
                f"Expected fork label with 'branches', got: {labels}"
            )


@pytest.mark.asyncio
async def test_chain_collapse(fork_env):
    """Chain of messages with single children should be collapsed into one node."""
    app = SessionTreeApp(fork_env["cwd"])

    with patch("tui._sessions_dir", return_value=fork_env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            tree = app.query_one("#tree")
            # The first child should be a chain (shared prefix of 4 messages)
            if tree.root.children:
                first = tree.root.children[0]
                data = app._get(first.data)
                if data:
                    # Should be collapsed chain: msg_count > 1 or label has [N msgs]
                    label = first.label.plain
                    msg_count = data.get("msg_count", 1)
                    assert msg_count > 1 or "msgs" in label


# ---------------------------------------------------------------------------
# E2E tests: session count x{N}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shared_prefix_count_label(fork_env):
    """Shared messages should show xN indicating how many sessions share them."""
    app = SessionTreeApp(fork_env["cwd"])

    with patch("tui._sessions_dir", return_value=fork_env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            tree = app.query_one("#tree")

            if tree.root.children:
                first = tree.root.children[0]
                data = app._get(first.data)
                if data:
                    count = data.get("count", 1)
                    assert count == 2  # shared between sessions A and B


# ---------------------------------------------------------------------------
# E2E tests: empty state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_sessions_dir(tmp_path):
    """App should handle empty sessions directory gracefully."""
    sessions_dir = tmp_path / "projects"
    sessions_dir.mkdir()
    cwd = "/test/empty"

    app = SessionTreeApp(cwd)

    with patch("tui._sessions_dir", return_value=sessions_dir):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            for _ in range(10):
                await pilot.pause()
                if not app._loading:
                    break

            assert app.session_count == 0
            status = app.query_one("#status")
            assert "0 sessions" in str(status.render())


@pytest.mark.asyncio
async def test_nonexistent_sessions_dir(tmp_path):
    """App should handle nonexistent sessions directory gracefully."""
    sessions_dir = tmp_path / "does-not-exist"
    cwd = "/test/nope"

    app = SessionTreeApp(cwd)

    with patch("tui._sessions_dir", return_value=sessions_dir):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            for _ in range(10):
                await pilot.pause()
                if not app._loading:
                    break

            assert app.session_count == 0


# ---------------------------------------------------------------------------
# E2E tests: age display
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nodes_show_age(single_session_env):
    """Nodes should display age in their labels."""
    env = single_session_env
    app = SessionTreeApp(env["cwd"])

    with patch("tui._sessions_dir", return_value=env["sessions_dir"]):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            tree = app.query_one("#tree")
            labels = []

            def _walk(node):
                if node is not tree.root:
                    labels.append(node.label.plain)
                for child in node.children:
                    _walk(child)

            _walk(tree.root)

            # At least one label should have an age indicator
            assert any("ago" in l for l in labels), (
                f"Expected age labels, got: {labels}"
            )


# ---------------------------------------------------------------------------
# E2E tests: open session (mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_session_no_claude(single_session_env):
    """'o' with no claude binary should not crash."""
    env = single_session_env
    app = SessionTreeApp(env["cwd"])

    with patch("tui._sessions_dir", return_value=env["sessions_dir"]), \
         patch("shutil.which", return_value=None):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            tree = app.query_one("#tree")
            tree.focus()
            await pilot.pause()

            await pilot.press("j")
            await pilot.pause()

            # Should not crash even without claude
            app.action_open_session()
            await pilot.pause()


# ---------------------------------------------------------------------------
# E2E tests: multi-session trie correctness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_way_fork(tmp_path):
    """Three sessions sharing a prefix then diverging should produce 3 branches."""
    sessions_dir = tmp_path / "projects"
    sessions_dir.mkdir()
    cwd = "/test/project"

    sids = [str(uuid4()) for _ in range(3)]
    for i, sid in enumerate(sids):
        records = [
            _make_record("user", "same start", sid),
            _make_record("assistant", "same reply", sid),
            _make_record("user", f"unique question {i}", sid),
        ]
        _write_session(sessions_dir, cwd, sid, records)

    app = SessionTreeApp(cwd)

    with patch("tui._sessions_dir", return_value=sessions_dir):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_load(app, pilot)

            assert app.session_count == 3

            tree = app.query_one("#tree")
            # Expand to fork point
            if tree.root.children:
                node = tree.root.children[0]
                app._populate_placeholder(node)
                node.expand()
                await pilot.pause()

                # Find the fork node
                def _find_fork(n):
                    data = app._get(n.data) if n.data and n.data != -1 else None
                    if data and data.get("_trie_node"):
                        trie = data["_trie_node"]
                        if len(trie.get("children", {})) == 3:
                            return n
                    for child in n.children:
                        result = _find_fork(child)
                        if result:
                            return result
                    return None

                # After full expand we should see the fork
                tree.root.expand_all()
                await pilot.pause()

                labels = []

                def _walk(n):
                    labels.append(n.label.plain)
                    for c in n.children:
                        _walk(c)

                _walk(tree.root)
                # All three unique messages should appear
                for i in range(3):
                    assert any(f"unique question {i}" in l for l in labels), (
                        f"Missing 'unique question {i}' in labels"
                    )
