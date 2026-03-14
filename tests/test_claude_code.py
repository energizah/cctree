"""Tests for the Claude Code backend plugin.

Tests are structured in layers:
1. Pure functions (no server, no subprocess)
2. JSONL import logic (filesystem, no subprocess)
3. Endpoint integration (TestClient, mocked subprocess)
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

# Set config path before importing app (avoids loading real config)
os.environ.setdefault("CANVAS_CHAT_CONFIG_PATH", "")

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lib" / "canvas-chat" / "src"))

from plugins.claude_code import (
    _extract_text_content,
    _find_session_file,
    _parse_session_file,
    _sessions_dir,
    _truncate,
    claude_code_import_dag,
    ImportDagRequest,
)


# =========================================================================
# 1. Pure function tests
# =========================================================================


class TestTruncate:
    def test_short_text_unchanged(self):
        assert _truncate("hello", 100) == "hello"

    def test_exact_limit_unchanged(self):
        text = "x" * 50
        assert _truncate(text, 50) == text

    def test_long_text_truncated(self):
        text = "x" * 200
        result = _truncate(text, 100)
        assert result.startswith("x" * 100)
        assert "truncated" in result
        assert "200 chars" in result

    def test_zero_limit(self):
        result = _truncate("hello", 0)
        assert "truncated" in result


class TestExtractTextContent:
    def test_string_content(self):
        msg = {"content": "hello world"}
        assert _extract_text_content(msg) == "hello world"

    def test_empty_content(self):
        msg = {"content": ""}
        assert _extract_text_content(msg) == ""

    def test_missing_content(self):
        msg = {}
        assert _extract_text_content(msg) == ""

    def test_text_block(self):
        msg = {"content": [{"type": "text", "text": "hello"}]}
        assert _extract_text_content(msg) == "hello"

    def test_multiple_text_blocks(self):
        msg = {
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "text", "text": "world"},
            ]
        }
        result = _extract_text_content(msg)
        assert "hello" in result
        assert "world" in result

    def test_tool_use_block(self):
        msg = {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Read",
                    "input": {"file_path": "/tmp/test.py"},
                }
            ]
        }
        result = _extract_text_content(msg)
        assert "Read" in result
        assert "/tmp/test.py" in result

    def test_tool_result_block(self):
        msg = {"content": [{"type": "tool_result", "output": "file contents here"}]}
        result = _extract_text_content(msg)
        assert "file contents" in result

    def test_tool_result_with_list_content(self):
        msg = {
            "content": [
                {
                    "type": "tool_result",
                    "content": [{"type": "text", "text": "result line"}],
                }
            ]
        }
        result = _extract_text_content(msg)
        assert "result line" in result

    def test_thinking_block_skipped(self):
        msg = {
            "content": [
                {"type": "thinking", "thinking": "internal monologue"},
                {"type": "text", "text": "visible response"},
            ]
        }
        result = _extract_text_content(msg)
        assert "internal monologue" not in result
        assert "visible response" in result

    def test_mixed_blocks(self):
        msg = {
            "content": [
                {"type": "text", "text": "I'll read the file."},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "x.py"}},
                {"type": "tool_result", "output": "contents"},
                {"type": "text", "text": "Here's what I found."},
            ]
        }
        result = _extract_text_content(msg)
        assert "I'll read the file." in result
        assert "Read" in result
        assert "Here's what I found." in result


class TestSessionsDir:
    def test_no_cwd_returns_base(self):
        result = _sessions_dir()
        assert result == Path.home() / ".claude" / "projects"

    def test_cwd_encodes_slashes(self):
        result = _sessions_dir("/home/adam/src/myproject")
        assert result.name == "-home-adam-src-myproject"
        assert result.parent == Path.home() / ".claude" / "projects"

    def test_cwd_encodes_dots(self):
        result = _sessions_dir("/home/adam/src/ArrayIdioms.jl")
        assert result.name == "-home-adam-src-ArrayIdioms-jl"


# =========================================================================
# 2. JSONL import logic tests
# =========================================================================


def _make_record(rtype, uuid, parent_uuid=None, content="test", **kwargs):
    """Build a minimal JSONL record."""
    rec = {
        "type": rtype,
        "uuid": uuid,
        "parentUuid": parent_uuid,
        "sessionId": "test-session",
        "timestamp": f"2026-01-01T00:00:{len(uuid) % 60:02d}.000Z",
        "message": {
            "role": "user" if rtype == "user" else "assistant",
            "content": content,
        },
    }
    if rtype == "assistant":
        rec["message"]["model"] = kwargs.get("model", "claude-opus-4-6")
    rec.update(kwargs)
    return rec


def _write_jsonl(path, records):
    """Write records as JSONL, including non-conversation records for realism."""
    with open(path, "w") as f:
        # File history snapshot (should be skipped)
        f.write(json.dumps({"type": "file-history-snapshot", "messageId": "x"}) + "\n")
        for rec in records:
            f.write(json.dumps(rec) + "\n")
        # System record (should be skipped)
        f.write(
            json.dumps({"type": "system", "subtype": "turn_duration", "durationMs": 100})
            + "\n"
        )


class TestFindSessionFile:
    def test_finds_by_cwd(self, tmp_path):
        cwd = "/home/adam/src/test"
        encoded = cwd.replace("/", "-")
        project_dir = tmp_path / encoded
        project_dir.mkdir()
        session_file = project_dir / "abc-123.jsonl"
        session_file.write_text("{}")

        with patch(
            "plugins.claude_code._sessions_dir",
            side_effect=lambda c=None: tmp_path / c.replace("/", "-") if c else tmp_path,
        ):
            result = _find_session_file("abc-123", cwd)
            assert result == session_file

    def test_finds_by_cwd_prefix(self, tmp_path):
        """Setting cwd to /home/adam finds sessions under -home-adam-src-foo."""
        project_dir = tmp_path / "-home-adam-src-foo"
        project_dir.mkdir()
        session_file = project_dir / "abc-123.jsonl"
        session_file.write_text("{}")

        with patch(
            "plugins.claude_code._sessions_dir",
            side_effect=lambda c=None: tmp_path / c.replace("/", "-") if c else tmp_path,
        ):
            result = _find_session_file("abc-123", "/home/adam")
            assert result == session_file

    def test_finds_across_projects(self, tmp_path):
        project_dir = tmp_path / "project-a"
        project_dir.mkdir()
        session_file = project_dir / "xyz-789.jsonl"
        session_file.write_text("{}")

        with patch("plugins.claude_code._sessions_dir", return_value=tmp_path):
            result = _find_session_file("xyz-789")
            assert result == session_file

    def test_returns_none_when_missing(self, tmp_path):
        with patch("plugins.claude_code._sessions_dir", return_value=tmp_path):
            result = _find_session_file("nonexistent")
            assert result is None


class TestImportParsing:
    """Test the JSONL → nodes/edges conversion logic via the endpoint."""

    @pytest.fixture
    def session_dir(self, tmp_path):
        """Create a temporary session directory."""
        project_dir = tmp_path / "-tmp-test"
        project_dir.mkdir()
        return project_dir

    def _import_session(self, session_dir, records, session_id="test-session"):
        """Write records and call the import endpoint synchronously."""
        _write_jsonl(session_dir / f"{session_id}.jsonl", records)

        # Import the parsing logic directly instead of going through HTTP
        # to avoid needing the full app server running.
        from plugins.claude_code import claude_code_import, ImportRequest
        import asyncio

        with patch(
            "plugins.claude_code._find_session_file",
            return_value=session_dir / f"{session_id}.jsonl",
        ):
            req = ImportRequest(session_id=session_id)
            result = asyncio.get_event_loop().run_until_complete(claude_code_import(req))
            return result

    def test_simple_conversation(self, session_dir):
        """User → assistant produces 2 nodes, 1 edge."""
        u1 = str(uuid4())
        a1 = str(uuid4())
        records = [
            _make_record("user", u1, content="hello"),
            _make_record("assistant", a1, parent_uuid=u1, content="hi there"),
        ]

        result = self._import_session(session_dir, records)
        assert len(result["nodes"]) == 2
        assert len(result["edges"]) == 1

        human_nodes = [n for n in result["nodes"] if n["type"] == "human"]
        ai_nodes = [n for n in result["nodes"] if n["type"] == "ai"]
        assert len(human_nodes) == 1
        assert len(ai_nodes) == 1
        assert human_nodes[0]["content"] == "hello"
        assert ai_nodes[0]["content"] == "hi there"

        edge = result["edges"][0]
        assert edge["source"] == human_nodes[0]["id"]
        assert edge["target"] == ai_nodes[0]["id"]
        assert edge["type"] == "reply"

    def test_assistant_coalescing(self, session_dir):
        """Multiple consecutive assistant records merge into one node."""
        u1 = str(uuid4())
        a1 = str(uuid4())
        a2 = str(uuid4())
        a3 = str(uuid4())

        records = [
            _make_record("user", u1, content="explain X"),
            _make_record(
                "assistant",
                a1,
                parent_uuid=u1,
                content=[{"type": "thinking", "thinking": "let me think"}],
            ),
            _make_record(
                "assistant",
                a2,
                parent_uuid=a1,
                content=[
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "x.py"}}
                ],
            ),
            _make_record(
                "assistant",
                a3,
                parent_uuid=a2,
                content=[{"type": "text", "text": "Here is the answer."}],
            ),
        ]

        result = self._import_session(session_dir, records)
        # Should coalesce a1+a2+a3 into a single AI node
        assert len(result["nodes"]) == 2  # 1 human + 1 coalesced AI
        assert len(result["edges"]) == 1

        ai_node = [n for n in result["nodes"] if n["type"] == "ai"][0]
        assert "Here is the answer" in ai_node["content"]

    def test_branching_detection(self, session_dir):
        """Two user messages sharing the same parent produce branch edges."""
        u1 = str(uuid4())
        a1 = str(uuid4())
        u2 = str(uuid4())
        u3 = str(uuid4())

        records = [
            _make_record("user", u1, content="start"),
            _make_record("assistant", a1, parent_uuid=u1, content="response"),
            _make_record("user", u2, parent_uuid=a1, content="branch A"),
            _make_record("user", u3, parent_uuid=a1, content="branch B"),
        ]

        result = self._import_session(session_dir, records)
        assert len(result["nodes"]) == 4

        # Edges from a1 to u2 and u3 should be "branch" type
        branch_edges = [e for e in result["edges"] if e["type"] == "branch"]
        assert len(branch_edges) == 2

    def test_skips_non_conversation_records(self, session_dir):
        """file-history-snapshot and system records are ignored."""
        u1 = str(uuid4())
        records = [_make_record("user", u1, content="only message")]

        result = self._import_session(session_dir, records)
        assert len(result["nodes"]) == 1

    def test_empty_session(self, session_dir):
        """Session with no user/assistant records returns empty."""
        result = self._import_session(session_dir, [])
        assert result["nodes"] == []
        assert result["edges"] == []

    def test_multi_turn_conversation(self, session_dir):
        """Multi-turn: u1 → a1 → u2 → a2 produces 4 nodes, 3 edges."""
        u1 = str(uuid4())
        a1 = str(uuid4())
        u2 = str(uuid4())
        a2 = str(uuid4())

        records = [
            _make_record("user", u1, content="first question"),
            _make_record("assistant", a1, parent_uuid=u1, content="first answer"),
            _make_record("user", u2, parent_uuid=a1, content="follow up"),
            _make_record("assistant", a2, parent_uuid=u2, content="second answer"),
        ]

        result = self._import_session(session_dir, records)
        assert len(result["nodes"]) == 4
        assert len(result["edges"]) == 3
        assert all(e["type"] == "reply" for e in result["edges"])

    def test_positions_increase(self, session_dir):
        """Nodes get increasing y positions."""
        u1 = str(uuid4())
        a1 = str(uuid4())
        u2 = str(uuid4())

        records = [
            _make_record("user", u1, content="one"),
            _make_record("assistant", a1, parent_uuid=u1, content="two"),
            _make_record("user", u2, parent_uuid=a1, content="three"),
        ]

        result = self._import_session(session_dir, records)
        ys = [n["position"]["y"] for n in result["nodes"]]
        assert ys == sorted(ys)
        assert len(set(ys)) == 3  # All different

    def test_node_has_session_metadata(self, session_dir):
        """Imported nodes carry claude_uuid and session_id."""
        u1 = str(uuid4())
        records = [_make_record("user", u1, content="test")]

        result = self._import_session(session_dir, records)
        node = result["nodes"][0]
        assert node["claude_uuid"] == u1
        assert node["session_id"] == "test-session"


# =========================================================================
# 3. Sessions listing tests
# =========================================================================


class TestSessionsListing:
    def test_lists_sessions(self, tmp_path):
        project_dir = tmp_path / "-home-test"
        project_dir.mkdir()

        u1 = str(uuid4())
        _write_jsonl(
            project_dir / "sess-1.jsonl",
            [
                _make_record("user", u1, content="hello world"),
                _make_record("assistant", str(uuid4()), parent_uuid=u1, content="hi"),
            ],
        )

        from plugins.claude_code import claude_code_sessions
        import asyncio

        with patch("plugins.claude_code._sessions_dir", return_value=tmp_path):
            result = asyncio.get_event_loop().run_until_complete(
                claude_code_sessions(cwd=None)
            )

        assert len(result) == 1
        assert result[0]["session_id"] == "sess-1"
        assert result[0]["first_prompt"] == "hello world"
        assert result[0]["message_count"] == 2

    def test_empty_directory(self, tmp_path):
        from plugins.claude_code import claude_code_sessions
        import asyncio

        with patch("plugins.claude_code._sessions_dir", return_value=tmp_path):
            result = asyncio.get_event_loop().run_until_complete(
                claude_code_sessions(cwd=None)
            )

        assert result == []

    def test_skips_empty_sessions(self, tmp_path):
        """Sessions with only system records should be excluded."""
        project_dir = tmp_path / "-home-test"
        project_dir.mkdir()

        # Write a session with only non-conversation records
        with open(project_dir / "empty-sess.jsonl", "w") as f:
            f.write(json.dumps({"type": "file-history-snapshot", "messageId": "x"}) + "\n")
            f.write(json.dumps({"type": "system", "subtype": "turn_duration"}) + "\n")

        from plugins.claude_code import claude_code_sessions
        import asyncio

        with patch("plugins.claude_code._sessions_dir", return_value=tmp_path):
            result = asyncio.get_event_loop().run_until_complete(
                claude_code_sessions(cwd=None)
            )

        assert len(result) == 0


# =========================================================================
# 4. DAG import tests (cross-session fork detection + chain collapsing)
# =========================================================================


class TestImportDag:
    @pytest.fixture
    def dag_dir(self, tmp_path):
        """Create a project dir for DAG tests."""
        project_dir = tmp_path / "-tmp-dagtest"
        project_dir.mkdir()
        return project_dir

    def _run_dag(self, tmp_path):
        import asyncio

        with patch("plugins.claude_code._sessions_dir", return_value=tmp_path):
            req = ImportDagRequest(cwd="/tmp/dagtest")
            return asyncio.get_event_loop().run_until_complete(
                claude_code_import_dag(req)
            )

    def test_two_forked_sessions_share_prefix(self, tmp_path, dag_dir):
        """Two sessions with identical first 2 messages diverge at msg 3."""
        u1, a1 = str(uuid4()), str(uuid4())

        u2a, a2a = str(uuid4()), str(uuid4())
        _write_jsonl(dag_dir / "sess-a.jsonl", [
            _make_record("user", u1, content="hello", sessionId="sess-a"),
            _make_record("assistant", a1, parent_uuid=u1, content="hi", sessionId="sess-a"),
            _make_record("user", u2a, parent_uuid=a1, content="branch A", sessionId="sess-a"),
            _make_record("assistant", a2a, parent_uuid=u2a, content="reply A", sessionId="sess-a"),
        ])

        u2b, a2b = str(uuid4()), str(uuid4())
        _write_jsonl(dag_dir / "sess-b.jsonl", [
            _make_record("user", u1, content="hello", sessionId="sess-b"),
            _make_record("assistant", a1, parent_uuid=u1, content="hi", sessionId="sess-b"),
            _make_record("user", u2b, parent_uuid=a1, content="branch B", sessionId="sess-b"),
            _make_record("assistant", a2b, parent_uuid=u2b, content="reply B", sessionId="sess-b"),
        ])

        result = self._run_dag(tmp_path)
        assert result["session_count"] == 2
        tree = result["tree"]
        # Shared prefix should show ×2
        assert "×2" in tree
        # Both branches should appear
        assert "branch A" in tree
        assert "branch B" in tree

    def test_no_shared_prefix(self, tmp_path, dag_dir):
        """Two sessions with different first messages produce independent lines."""
        _write_jsonl(dag_dir / "sess-a.jsonl", [
            _make_record("user", str(uuid4()), content="apple", sessionId="sess-a"),
        ])
        _write_jsonl(dag_dir / "sess-b.jsonl", [
            _make_record("user", str(uuid4()), content="banana", sessionId="sess-b"),
        ])

        result = self._run_dag(tmp_path)
        assert result["session_count"] == 2
        tree = result["tree"]
        assert "apple" in tree
        assert "banana" in tree

    def test_long_linear_collapses(self, tmp_path, dag_dir):
        """A single session with 10 messages collapses to one line."""
        records = []
        prev = None
        for i in range(10):
            uid = str(uuid4())
            rtype = "user" if i % 2 == 0 else "assistant"
            records.append(
                _make_record(rtype, uid, parent_uuid=prev, content=f"msg {i}", sessionId="sess-a")
            )
            prev = uid

        _write_jsonl(dag_dir / "sess-a.jsonl", records)

        result = self._run_dag(tmp_path)
        assert result["session_count"] == 1
        tree = result["tree"]
        # 10 messages should collapse to a single line with [10 msgs]
        assert "[10 msgs]" in tree
        lines = [l for l in tree.split("\n") if l.strip()]
        assert len(lines) == 1

    def test_tree_lines_populated(self, tmp_path, dag_dir):
        """tree_lines array contains structured data for each tree line."""
        u1, a1 = str(uuid4()), str(uuid4())

        _write_jsonl(dag_dir / "sess-a.jsonl", [
            _make_record("user", u1, content="hello", sessionId="sess-a"),
            _make_record("assistant", a1, parent_uuid=u1, content="world", sessionId="sess-a"),
        ])

        result = self._run_dag(tmp_path)
        tree_lines = result["tree_lines"]
        assert len(tree_lines) >= 1

        line = tree_lines[0]
        assert "prefix" in line
        assert "connector" in line
        assert "text" in line
        assert "session_ids" in line
        assert "count" in line
        assert "msg_count" in line
        assert "sess-a" in line["session_ids"]

    def test_tree_lines_clickable_branches(self, tmp_path, dag_dir):
        """Forked sessions produce tree_lines with distinct session_ids."""
        u1, a1 = str(uuid4()), str(uuid4())

        _write_jsonl(dag_dir / "sess-a.jsonl", [
            _make_record("user", u1, content="hello", sessionId="sess-a"),
            _make_record("assistant", a1, parent_uuid=u1, content="hi", sessionId="sess-a"),
            _make_record("user", str(uuid4()), parent_uuid=a1, content="branch A", sessionId="sess-a"),
        ])

        _write_jsonl(dag_dir / "sess-b.jsonl", [
            _make_record("user", u1, content="hello", sessionId="sess-b"),
            _make_record("assistant", a1, parent_uuid=u1, content="hi", sessionId="sess-b"),
            _make_record("user", str(uuid4()), parent_uuid=a1, content="branch B", sessionId="sess-b"),
        ])

        result = self._run_dag(tmp_path)
        tree_lines = result["tree_lines"]

        # Should have at least 3 lines: shared trunk + 2 branches
        assert len(tree_lines) >= 3

        # Find the branch lines
        branch_lines = [tl for tl in tree_lines if "branch" in tl["text"]]
        assert len(branch_lines) == 2

        # Each branch line should have a session_id
        for bl in branch_lines:
            assert len(bl["session_ids"]) >= 1
