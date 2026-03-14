"""Claude Code Session Tree TUI.

Interactive terminal tree view of all Claude Code sessions under a working
directory.  Reuses the backend's trie-building logic to detect forks and
collapse linear chains, then renders the result in a Textual Tree widget
with clickable nodes that import sessions.

Usage:
    python tui.py /path/to/project
    python tui.py                    # uses cwd
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Footer, Header, Input, Static, Tree
from textual import work


# ---------------------------------------------------------------------------
# Pure helpers (copied from plugins/claude_code.py to avoid importing FastAPI)
# ---------------------------------------------------------------------------


def _encode_cwd(cwd: str) -> str:
    return re.sub(r"[^a-zA-Z0-9\-]", "-", cwd)


def _sessions_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _find_session_file(session_id: str, cwd: str) -> Path | None:
    """Locate the JSONL file for a given session ID under cwd."""
    base = _sessions_dir()
    encoded = _encode_cwd(cwd)
    for d in base.iterdir():
        if not d.is_dir() or not d.name.startswith(encoded):
            continue
        p = d / f"{session_id}.jsonl"
        if p.exists():
            return p
    return None


def _rewind_session_file(path: Path, target_hash: str) -> bool:
    """Truncate a session JSONL in-place up to the message matching target_hash.

    Replays coalescing to compute content hashes, finds the raw line matching
    the target, and rewrites the file with only records up to that point.
    Returns True on success.
    """
    with open(path) as f:
        raw_lines = f.readlines()

    # Parse user/assistant records, tracking line indices
    records = []  # (rec, line_idx)
    record_by_uuid = {}

    for i, line in enumerate(raw_lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            rec = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if rec.get("type") not in ("user", "assistant"):
            continue
        records.append((rec, i))
        uid = rec.get("uuid")
        if uid:
            record_by_uuid[uid] = rec

    # Replay coalescing to compute content hashes per coalesced message
    coalesced = []  # list of {message, _root_uuid, _last_line}
    merge_target = {}

    for rec, line_idx in records:
        rtype = rec.get("type")
        uid = rec.get("uuid")
        parent_uuid = rec.get("parentUuid")

        if rtype == "assistant" and parent_uuid and parent_uuid in record_by_uuid:
            parent_rec = record_by_uuid[parent_uuid]
            if parent_rec.get("type") == "assistant":
                root = parent_uuid
                while root in merge_target:
                    root = merge_target[root]
                merge_target[uid] = root

                for entry in coalesced:
                    if entry["_root_uuid"] == root:
                        entry["_last_line"] = line_idx
                        root_msg = entry["message"]
                        root_content = root_msg.get("content", [])
                        new_content = rec.get("message", {}).get("content", [])
                        if isinstance(root_content, str):
                            root_content = [{"type": "text", "text": root_content}]
                        if isinstance(new_content, str):
                            new_content = [{"type": "text", "text": new_content}]
                        root_msg["content"] = root_content + new_content
                        break
                continue

        if rtype == "user" or (rtype == "assistant" and uid not in merge_target.values()):
            if uid not in merge_target:
                coalesced.append({
                    "type": rtype,
                    "message": {**rec.get("message", {})},
                    "_root_uuid": uid or "",
                    "_last_line": line_idx,
                })

    # Find cutoff line for target_hash
    cutoff_line = None
    for entry in coalesced:
        msg = entry["message"]
        role = msg.get("role", "")
        content = msg.get("content", "")
        raw = json.dumps(content, sort_keys=True)
        h = hashlib.sha256(f"{role}:{raw}".encode()).hexdigest()[:16]
        cutoff_line = entry["_last_line"]
        if h == target_hash:
            break
    else:
        return False

    # Rewrite file truncated at cutoff
    with open(path, "w") as f:
        for i, line in enumerate(raw_lines):
            if i > cutoff_line:
                break
            f.write(line)

    return True



def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"\n\n... (truncated, {len(text)} chars total)"


def _extract_text_content(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            btype = block.get("type", "")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_use":
                name = block.get("name", "unknown")
                inp = block.get("input", {})
                inp_str = json.dumps(inp, indent=2) if isinstance(inp, dict) else str(inp)
                parts.append(f"**Tool: {name}**\n```json\n{inp_str}\n```")
            elif btype == "tool_result":
                out = block.get("output", block.get("content", ""))
                if isinstance(out, list):
                    out = "\n".join(
                        b.get("text", "") for b in out if isinstance(b, dict)
                    )
                parts.append(f"**Tool result:**\n```\n{out}\n```")
    return "\n\n".join(parts)


def _parse_session_file(path: Path) -> list[dict]:
    records: list[dict] = []
    record_by_uuid: dict[str, dict] = {}

    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rtype = rec.get("type")
            if rtype not in ("user", "assistant"):
                continue
            records.append(rec)
            uuid = rec.get("uuid")
            if uuid:
                record_by_uuid[uuid] = rec

    coalesced: list[dict] = []
    merge_target: dict[str, str] = {}

    for rec in records:
        rtype = rec.get("type")
        uuid = rec.get("uuid")
        parent_uuid = rec.get("parentUuid")

        if rtype == "assistant" and parent_uuid and parent_uuid in record_by_uuid:
            parent_rec = record_by_uuid[parent_uuid]
            if parent_rec.get("type") == "assistant":
                root = parent_uuid
                while root in merge_target:
                    root = merge_target[root]
                merge_target[uuid] = root

                root_entry = None
                for entry in coalesced:
                    if entry.get("_root_uuid") == root:
                        root_entry = entry
                        break
                if root_entry is None:
                    root_rec = record_by_uuid[root]
                    root_entry = {**root_rec, "_root_uuid": root}
                    for i, entry in enumerate(coalesced):
                        if entry.get("uuid") == root:
                            coalesced[i] = root_entry
                            break

                root_msg = root_entry["message"]
                root_content = root_msg.get("content", [])
                new_content = rec.get("message", {}).get("content", [])
                if isinstance(root_content, str):
                    root_content = [{"type": "text", "text": root_content}]
                if isinstance(new_content, str):
                    new_content = [{"type": "text", "text": new_content}]
                root_msg["content"] = root_content + new_content
                continue

        if rtype == "user" or (rtype == "assistant" and uuid not in merge_target.values()):
            if uuid not in merge_target:
                coalesced.append(rec)

    result = []
    for rec in coalesced:
        msg = rec.get("message", {})
        role = msg.get("role", "")
        content = msg.get("content", "")
        raw = json.dumps(content, sort_keys=True)
        h = hashlib.sha256(f"{role}:{raw}".encode()).hexdigest()[:16]
        result.append({
            "type": rec.get("type"),
            "message": msg,
            "timestamp": rec.get("timestamp", ""),
            "content_hash": h,
            "session_id": rec.get("sessionId", ""),
        })

    return result


# ---------------------------------------------------------------------------
# Trie builder (extracted from import-dag endpoint)
# ---------------------------------------------------------------------------


def _build_trie(cwd: str) -> tuple[dict, int]:
    """Build a trie from all sessions under cwd.  Returns (trie_root, session_count)."""
    base = _sessions_dir()
    if not base.exists():
        return {"children": {}, "messages": [], "count": 0, "session_ids": set()}, 0

    encoded = _encode_cwd(cwd)
    dirs = [d for d in base.iterdir() if d.is_dir() and d.name.startswith(encoded)]

    all_sessions: list[list[dict]] = []
    for d in dirs:
        for jsonl_file in d.glob("*.jsonl"):
            msgs = _parse_session_file(jsonl_file)
            if msgs:
                file_sid = jsonl_file.stem
                for msg in msgs:
                    msg["file_session_id"] = file_sid
                all_sessions.append(msgs)

    if not all_sessions:
        return {"children": {}, "messages": [], "count": 0, "session_ids": set()}, 0

    all_sessions.sort(key=len, reverse=True)

    trie_root: dict = {"children": {}, "messages": [], "count": 0, "session_ids": set()}

    for session_msgs in all_sessions:
        node = trie_root
        for msg in session_msgs:
            h = msg["content_hash"]
            if h not in node["children"]:
                node["children"][h] = {
                    "children": {},
                    "messages": [msg],
                    "count": 0,
                    "session_ids": set(),
                }
            node = node["children"][h]
            node["count"] += 1
            node["session_ids"].add(msg.get("file_session_id", msg["session_id"]))

    return trie_root, len(all_sessions)


def _msg_role(msg: dict) -> tuple[str, str]:
    """Return (role_char, style) for a message, distinguishing tool results from human."""
    if msg["type"] == "assistant":
        return "A", "bold green"
    # "user" type — check if it's actually a tool_result
    content = msg["message"].get("content", "")
    if isinstance(content, list):
        if all(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in content
        ):
            return "T", "bold magenta"
    return "H", "bold cyan"


def _preview(msg: dict) -> str:
    """One-line preview of a message (no truncation — tree widget clips)."""
    content = msg["message"].get("content", "")
    if isinstance(content, str):
        preview = content
    else:
        preview = ""
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                preview = block.get("text", "")
                break
            if isinstance(block, dict) and block.get("type") == "tool_use":
                preview = f"[{block.get('name', 'tool')}]"
                break
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool_content = block.get("content", block.get("output", ""))
                if isinstance(tool_content, list):
                    tool_content = " ".join(
                        b.get("text", "") for b in tool_content if isinstance(b, dict)
                    )
                preview = str(tool_content)
                break
    preview = preview.replace("\n", " ").strip()
    return preview


# ---------------------------------------------------------------------------
# Message detail panel
# ---------------------------------------------------------------------------


def _format_detail(msg: dict, max_chars: int = 2000) -> Text:
    """Format a message for the detail panel as highlighted Rich Text."""
    from rich.syntax import Syntax

    role = msg.get("type", "unknown")
    content = _extract_text_content(msg.get("message", {}))
    content = _truncate(content, max_chars)
    session = msg.get("session_id", "?")
    ts = msg.get("timestamp", "")

    result = Text()
    result.append(f"[{role.upper()}]", style="bold")
    result.append(f"  session={session}  ts={ts}\n", style="dim")
    result.append("=" * 60 + "\n", style="dim")

    # Split on fenced code blocks and highlight them
    parts = re.split(r"(```\w*\n.*?```)", content, flags=re.DOTALL)
    for part in parts:
        m = re.match(r"```(\w*)\n(.*?)```", part, flags=re.DOTALL)
        if m:
            lang = m.group(1) or "text"
            code = m.group(2)
            # For Write/Edit tool JSON, extract embedded code by file extension
            if lang == "json":
                code, lang = _extract_tool_code(code, lang)
            syntax = Syntax(code, lang, theme="monokai", line_numbers=False)
            result.append_text(syntax.highlight(code))
        else:
            result.append(part)

    return result


# Map file extensions to Pygments language names
_EXT_TO_LANG = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".jsx": "jsx", ".tsx": "tsx", ".jl": "julia", ".rs": "rust",
    ".go": "go", ".rb": "ruby", ".sh": "bash", ".bash": "bash",
    ".zsh": "zsh", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".json": "json", ".md": "markdown", ".html": "html", ".css": "css",
    ".sql": "sql", ".lua": "lua", ".nix": "nix", ".c": "c",
    ".cpp": "cpp", ".h": "c", ".hpp": "cpp", ".java": "java",
    ".kt": "kotlin", ".swift": "swift", ".ex": "elixir",
    ".erl": "erlang", ".hs": "haskell", ".ml": "ocaml",
}


def _extract_tool_code(code: str, fallback_lang: str) -> tuple[str, str]:
    """For Write/Edit tool JSON, pull out the embedded code and detect language."""
    try:
        obj = json.loads(code)
    except (json.JSONDecodeError, ValueError):
        # JSON string values may contain unescaped newlines from content extraction;
        # re-escape them inside quoted strings and retry
        fixed = re.sub(
            r'"((?:[^"\\]|\\.)*?)"',
            lambda m: '"' + m.group(1).replace("\n", "\\n").replace("\t", "\\t") + '"',
            code,
            flags=re.DOTALL,
        )
        try:
            obj = json.loads(fixed)
        except (json.JSONDecodeError, ValueError):
            return code, fallback_lang

    if not isinstance(obj, dict):
        return code, fallback_lang

    file_path = obj.get("file_path", "")
    ext = Path(file_path).suffix.lower() if file_path else ""
    lang = _EXT_TO_LANG.get(ext, fallback_lang)

    # Write tool: "content" has the full file
    if "content" in obj:
        return obj["content"], lang

    # Edit tool: show old_string -> new_string
    old = obj.get("old_string", "")
    new = obj.get("new_string", "")
    if old or new:
        parts = []
        if old:
            parts.append(f"--- old ---\n{old}")
        if new:
            parts.append(f"+++ new +++\n{new}")
        return "\n\n".join(parts), lang

    return code, fallback_lang


# ---------------------------------------------------------------------------
# Textual App
# ---------------------------------------------------------------------------


class SessionTreeApp(App):
    """Interactive session tree viewer."""

    TITLE = "Claude Code Sessions"
    CSS = """
    #tree {
        width: 1fr;
        min-width: 40;
        border-right: solid $accent;
    }
    #detail {
        width: 2fr;
        padding: 1 2;
        overflow-y: auto;
    }
    #status {
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
    }
    #main {
        layout: horizontal;
        height: 1fr;
    }
    #chat-input {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("e", "expand_all", "Expand all"),
        Binding("c", "collapse_all", "Collapse all"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("h", "collapse_node", "Collapse", show=False),
        Binding("l", "expand_node", "Expand", show=False),
        Binding("ctrl+d", "page_down", "Page down", show=False),
        Binding("ctrl+u", "page_up", "Page up", show=False),
        Binding("g", "go_top", "Top", show=False),
        Binding("G", "go_bottom", "Bottom", show=False),
        Binding("p", "toggle_detail", "Toggle detail"),
        Binding("y", "yank_detail", "Copy detail"),
        Binding("i", "focus_input", "Chat"),
    ]

    def __init__(self, cwd: str):
        super().__init__()
        self.cwd = cwd
        self.trie_root: dict = {}
        self.session_count = 0
        self._pending_detail = None
        self._detail_timer = None
        self._node_data: dict[int, dict] = {}  # id -> heavy data
        self._next_id = 0
        self._streaming = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            yield Tree("Sessions", id="tree")
            yield Static("Select a node to view details", id="detail")
        yield Input(
            placeholder="Send a message (Enter to send, selected node = resume context)",
            id="chat-input",
        )
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        tree = self.query_one("#tree", Tree)
        tree.root.set_label(Text(f" {self.cwd}", style="bold"))
        tree.root.expand()
        tree.root.add_leaf(Text("Loading sessions...", style="dim italic"))
        status = self.query_one("#status", Static)
        status.update(" Loading sessions...")
        self.load_tree()

    @work(thread=True, group="load_tree")
    def load_tree(self, select_session: str | None = None) -> None:
        """Parse sessions in a worker thread so the UI stays responsive."""
        self.log.info(f"[{self._ts()}] load_tree start select={select_session and select_session[:8]}")
        self.trie_root, self.session_count = _build_trie(self.cwd)
        self.log.info(f"[{self._ts()}] load_tree trie built")
        self.call_from_thread(self._render_tree, select_session)

    def _render_tree(self, select_session: str | None = None) -> None:
        self.log.info(f"[{self._ts()}] _render_tree start select={select_session and select_session[:8]}")
        tree = self.query_one("#tree", Tree)
        tree.root.remove_children()
        self._add_trie_children(tree.root, self.trie_root)

        status = self.query_one("#status", Static)
        node_count = self._count_nodes(self.trie_root)
        status.update(f" {self.session_count} sessions, {node_count} messages")
        self.log.info(f"[{self._ts()}] _render_tree done")

        if select_session:
            self._select_session(select_session)

    def _count_nodes(self, trie_node: dict) -> int:
        count = 0
        stack = [trie_node]
        while stack:
            node = stack.pop()
            for child in node["children"].values():
                count += 1
                stack.append(child)
        return count

    def _store(self, data: dict) -> int:
        """Store heavy data in side dict, return lightweight ID."""
        nid = self._next_id
        self._next_id += 1
        self._node_data[nid] = data
        return nid

    def _get(self, nid) -> dict | None:
        """Retrieve heavy data by ID."""
        if isinstance(nid, int):
            return self._node_data.get(nid)
        return None

    def _select_session(self, session_id: str) -> None:
        """Expand the tree along the path of a session and select the deepest node."""
        self.log.info(f"[{self._ts()}] _select_session {session_id[:8]}")
        tree = self.query_one("#tree", Tree)

        def _walk(node):
            """DFS: find deepest node containing session_id, expanding along the way."""
            best = None
            for child in list(node.children):
                data = self._get(child.data)
                if not data:
                    continue
                if session_id not in data.get("session_ids", []):
                    continue

                # This node is on the path — eagerly populate and expand it
                self._expand_node_now(child)

                # Try to go deeper
                deeper = _walk(child)
                best = deeper or child

            return best

        target = _walk(tree.root)
        if target:
            tree.select_node(target)

    def _populate_placeholder(self, node) -> None:
        """Replace placeholder child with real chain segments and trie children."""
        data = self._get(node.data)
        if not data:
            return

        children = list(node.children)
        if len(children) != 1 or children[0].data != -1:
            return  # already populated

        children[0].remove()

        chain = data.get("chain")
        trie_node = data.get("_trie_node")

        if chain:
            for i, seg in enumerate(chain):
                msg = seg["messages"][0]
                preview_text = _preview(msg)
                role, role_style = _msg_role(msg)
                msg_label = Text()
                msg_label.append(f"{role}: ", style=role_style)
                msg_label.append(preview_text)
                seg_session_ids = sorted(seg["session_ids"] - {""})
                msg_data = {
                    "session_ids": seg_session_ids,
                    "first_msg": msg,
                    "last_msg": msg,
                    "msg_count": 1,
                    "count": seg["count"],
                }

                is_last = (i == len(chain) - 1)
                if is_last and trie_node:
                    # Last segment gets trie branches as children
                    msg_data["_trie_node"] = trie_node
                    nid = self._store(msg_data)
                    last_node = node.add(msg_label, data=nid)
                    last_node.add_leaf(Text("...", style="dim"), data=-1)
                else:
                    nid = self._store(msg_data)
                    node.add_leaf(msg_label, data=nid)
            data["chain"] = None
            data["_trie_node"] = None
        elif trie_node:
            self._add_trie_children(node, trie_node)
            data["_trie_node"] = None

    def _expand_node_now(self, node) -> None:
        """Eagerly populate and expand a node (bypasses async message dispatch)."""
        self._populate_placeholder(node)
        node.expand()

    def _add_trie_children(self, parent_tree_node, trie_node: dict) -> None:
        """Add one level of trie children to a Textual tree node (lazy)."""
        for _h, child in trie_node["children"].items():
            # Collapse linear chains
            chain = [child]
            cur = child
            while len(cur["children"]) == 1:
                cur = next(iter(cur["children"].values()))
                chain.append(cur)

            msg = chain[0]["messages"][0]
            preview_text = _preview(msg)
            count = chain[0]["count"]
            n_msgs = len(chain)

            role, role_style = _msg_role(msg)
            label = Text()
            if n_msgs == 1:
                label.append(f"{role}: ", style=role_style)
                label.append(preview_text)
            else:
                label.append(f"[{n_msgs} msgs] ", style="bold yellow")
                label.append(f"{role}: ", style=role_style)
                label.append(preview_text)

            if count > 1:
                label.append(f"  \u00d7{count}", style="dim")

            chain_session_ids: set[str] = set()
            for seg in chain:
                chain_session_ids.update(seg["session_ids"])
            chain_session_ids.discard("")

            end_node = chain[-1]
            has_children = bool(end_node["children"])

            all_msgs = [seg["messages"][0] for seg in chain]
            data = {
                "session_ids": sorted(chain_session_ids),
                "first_msg": chain[0]["messages"][0],
                "last_msg": chain[-1]["messages"][0],
                "msgs": all_msgs,
                "msg_count": n_msgs,
                "count": count,
                "chain": chain if n_msgs > 1 else None,
                "_trie_node": end_node if has_children else None,
                "_hash_key": _h,
            }
            nid = self._store(data)

            if has_children or n_msgs > 1:
                child_tree_node = parent_tree_node.add(label, data=nid)
                child_tree_node.add_leaf(
                    Text("...", style="dim"),
                    data=-1,  # placeholder sentinel
                )
            else:
                parent_tree_node.add_leaf(label, data=nid)

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        """Lazily populate children on first expand."""
        self._populate_placeholder(event.node)

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        """Show message detail when cursor moves to a node."""
        self.log.info(f"highlighted: node.data={event.node.data}")
        self._pending_detail = self._get(event.node.data)
        if self._detail_timer:
            self._detail_timer.stop()
        self._detail_timer = self.set_timer(0.02, self._flush_detail)

    def _flush_detail(self) -> None:
        self.log.info("flush_detail fired")
        detail = self.query_one("#detail", Static)
        data = self._pending_detail
        if not data:
            detail.update("(root node)")
            return

        session_ids = data.get("session_ids", [])
        msg_count = data.get("msg_count", 1)
        count = data.get("count", 1)

        result = Text()
        result.append(f"Sessions: {', '.join(s[:8] for s in session_ids)}\n")
        result.append(f"Messages in chain: {msg_count}\n")
        if count > 1:
            result.append(f"Shared across: {count} sessions\n")
        result.append("\n")

        msgs = data.get("msgs") or []
        if not msgs:
            first_msg = data.get("first_msg")
            if first_msg:
                msgs = [first_msg]

        # Lazy: only format enough messages to fill the visible area
        budget = max(self.size.height - 6, 10)  # lines available
        lines_used = 0
        for i, msg in enumerate(msgs):
            if i > 0:
                if lines_used >= budget:
                    remaining = len(msgs) - i
                    result.append(f"\n\n... {remaining} more message(s)", style="dim")
                    break
                result.append("\n\n")
                lines_used += 2
            formatted = _format_detail(msg)
            lines_used += formatted.plain.count("\n") + 1
            result.append_text(formatted)

        detail.update(result)

    def action_cursor_down(self) -> None:
        self.log.info("cursor_down")
        tree = self.query_one("#tree", Tree)
        tree.action_cursor_down()

    def action_cursor_up(self) -> None:
        self.log.info("cursor_up")
        tree = self.query_one("#tree", Tree)
        tree.action_cursor_up()

    def action_expand_node(self) -> None:
        tree = self.query_one("#tree", Tree)
        node = tree.cursor_node
        if node and not node.is_expanded:
            node.expand()
        else:
            # If already expanded, move to first child
            tree.action_cursor_down()

    def action_collapse_node(self) -> None:
        tree = self.query_one("#tree", Tree)
        node = tree.cursor_node
        if node and node.is_expanded:
            node.collapse()
        elif node and node.parent:
            # Move to parent
            tree.select_node(node.parent)

    def action_page_down(self) -> None:
        tree = self.query_one("#tree", Tree)
        for _ in range(tree.size.height // 2):
            tree.action_cursor_down()

    def action_page_up(self) -> None:
        tree = self.query_one("#tree", Tree)
        for _ in range(tree.size.height // 2):
            tree.action_cursor_up()

    def action_go_top(self) -> None:
        tree = self.query_one("#tree", Tree)
        tree.scroll_home()
        if tree.root.children:
            tree.select_node(tree.root.children[0])

    def action_go_bottom(self) -> None:
        tree = self.query_one("#tree", Tree)
        tree.scroll_end()
        # Find last visible node
        node = tree.root
        while node.children:
            last = list(node.children)[-1]
            if not last.is_expanded:
                node = last
                break
            node = last
        if node != tree.root:
            tree.select_node(node)

    def action_toggle_detail(self) -> None:
        detail = self.query_one("#detail", Static)
        detail.display = not detail.display

    def action_yank_detail(self) -> None:
        tree = self.query_one("#tree", Tree)
        node = tree.cursor_node
        data = self._get(node.data) if node else None
        if not data:
            return
        msgs = data.get("msgs") or []
        if not msgs:
            first_msg = data.get("first_msg")
            if first_msg:
                msgs = [first_msg]
        if not msgs:
            return
        result = _format_detail(msgs[0])
        for msg in msgs[1:]:
            result.append("\n\n")
            result.append_text(_format_detail(msg))
        self.copy_to_clipboard(result.plain)
        self.notify("Copied to clipboard")

    def action_focus_input(self) -> None:
        self.query_one("#chat-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        prompt = event.value.strip()
        if not prompt or self._streaming:
            return
        event.input.value = ""

        # Get session_id and content_hash from selected tree node (if any)
        tree = self.query_one("#tree", Tree)
        node = tree.cursor_node
        data = self._get(node.data) if node else None
        session_id = data["session_ids"][0] if data and data.get("session_ids") else None
        # Use last_msg hash (end of chain) for rewind target
        content_hash = None
        if data:
            msg = data.get("last_msg") or data.get("first_msg")
            if msg:
                content_hash = msg.get("content_hash")

        # Show the user's message in the tree immediately (before worker starts)
        self._add_pending_node(prompt)

        self._stream_chat(prompt, session_id, content_hash)

    def on_key(self, event) -> None:
        """Return focus to tree on Escape from input."""
        if event.key == "escape" and self.focused is self.query_one("#chat-input", Input):
            self.query_one("#tree", Tree).focus()

    def _ts(self) -> str:
        """Return a compact timestamp for logging."""
        from datetime import datetime
        return datetime.now().strftime("%H:%M:%S.%f")[:-3]

    @work(thread=True, group="stream_chat")
    def _stream_chat(self, prompt: str, session_id: str | None,
                     content_hash: str | None = None) -> None:
        claude = shutil.which("claude")
        if not claude:
            self.call_from_thread(self._update_status, "Error: 'claude' not found in PATH")
            return

        self.log.info(f"[{self._ts()}] _stream_chat start sid={session_id} hash={content_hash}")
        self._streaming = True
        self.call_from_thread(self._update_status, "Streaming...")
        chat_input = self.query_one("#chat-input", Input)
        self.call_from_thread(setattr, chat_input, "disabled", True)

        # Fork-rewind-resume: fork the session, rewind the fork to the
        # selected message, then resume from there.
        resume_id = None
        if session_id:
            self.call_from_thread(self._update_status, "Forking session...")
            self.log.info(f"[{self._ts()}] forking {session_id}")
            fork_id = self._fork_session(claude, session_id)
            self.log.info(f"[{self._ts()}] fork done -> {fork_id}")
            if fork_id and content_hash:
                # Rewind the forked copy to the target message
                fork_path = _find_session_file(fork_id, self.cwd)
                self.log.info(f"[{self._ts()}] rewinding {fork_id} to {content_hash}")
                if fork_path and _rewind_session_file(fork_path, content_hash):
                    resume_id = fork_id
                    self.log.info(f"[{self._ts()}] rewind ok")
                else:
                    resume_id = fork_id  # rewind failed, resume from tip
                    self.log.info(f"[{self._ts()}] rewind failed, using tip")
            elif fork_id:
                resume_id = fork_id
            else:
                resume_id = session_id  # fork failed, resume original
                self.log.info(f"[{self._ts()}] fork failed, using original")

        cmd = [
            claude, "--print", "--verbose", "--output-format", "stream-json",
            "--dangerously-skip-permissions",
        ]
        if resume_id:
            cmd.extend(["--resume", resume_id])
        cmd.append(prompt)

        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}

        try:
            self.log.info(f"[{self._ts()}] launching claude CLI")
            process = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, cwd=self.cwd,
            )

            text_so_far = ""
            for raw_line in process.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type")
                if etype == "assistant":
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            text_so_far += block.get("text", "")
                            self.call_from_thread(self._update_response, text_so_far)
                        elif block.get("type") == "tool_use":
                            self.log.info(f"[{self._ts()}] tool_use: {block.get('name', '')}")
                            self.call_from_thread(
                                self._update_status,
                                f"Using tool: {block.get('name', '')}",
                            )
                elif etype == "result":
                    sid = event.get("session_id")
                    cost = event.get("total_cost_usd")
                    self.log.info(f"[{self._ts()}] result sid={sid and sid[:8]} cost={cost}")
                    self.call_from_thread(self._finish_response, sid, cost)

            process.wait()
            if process.returncode != 0:
                stderr = process.stderr.read().decode("utf-8", errors="replace").strip()
                if stderr:
                    self.call_from_thread(self._update_status, f"Error: {stderr[:200]}")
        except Exception as exc:
            self.call_from_thread(self._update_status, f"Error: {exc}")
        finally:
            self._streaming = False
            self.call_from_thread(setattr, chat_input, "disabled", False)

    def _add_pending_node(self, prompt: str) -> None:
        """Add a temporary node showing the user's message under the current cursor."""
        tree = self.query_one("#tree", Tree)
        node = tree.cursor_node
        if not node or node is tree.root:
            return

        # Expand the current node if needed so the new child is visible
        self._populate_placeholder(node)
        if not node.allow_expand:
            # Convert leaf to branch by re-adding it — too complex.
            # Instead, just add to the parent.
            node = node.parent or tree.root

        label = Text()
        label.append("H: ", style="bold cyan")
        label.append(prompt)
        label.append("  ...", style="dim italic")
        pending = node.add_leaf(label, data=-2)  # -2 = pending sentinel
        node.expand()
        tree.select_node(pending)

    def _reload_tree_inline(self, select_session: str | None = None) -> None:
        """Rebuild trie in current thread and render on main thread.

        Unlike load_tree (which uses @work), this avoids worker group
        cancellation issues when called during streaming.
        """
        self.trie_root, self.session_count = _build_trie(self.cwd)
        self.call_from_thread(self._render_tree, select_session)

    def _fork_session(self, claude: str, session_id: str) -> str | None:
        """Fork a session via the CLI, returning the new session ID."""
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
        cmd = [
            claude, "--print", "--resume", session_id, "--fork-session",
            "--output-format", "json", "--dangerously-skip-permissions",
            ".",
        ]
        try:
            result = subprocess.run(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, cwd=self.cwd,
                timeout=30,
            )
            if result.returncode != 0:
                return None
            stdout = result.stdout.decode("utf-8", errors="replace").strip()
            # Parse JSON output for session_id
            for line in stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if "session_id" in data:
                        return data["session_id"]
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass
        return None

    def _update_response(self, text: str) -> None:
        detail = self.query_one("#detail", Static)
        detail.update(Text(text))

    def _update_status(self, text: str) -> None:
        status = self.query_one("#status", Static)
        status.update(f" {text}")

    def _finish_response(self, session_id: str | None, cost: float | None) -> None:
        self.log.info(f"[{self._ts()}] _finish_response sid={session_id and session_id[:8]} cost={cost}")
        parts = []
        if session_id:
            parts.append(f"session={session_id[:8]}")
        if cost is not None:
            parts.append(f"cost=${cost:.4f}")
        self._update_status(f"Done. {' '.join(parts)}")
        self.load_tree(select_session=session_id)

    def action_expand_all(self) -> None:
        tree = self.query_one("#tree", Tree)
        tree.root.expand_all()

    def action_collapse_all(self) -> None:
        tree = self.query_one("#tree", Tree)
        tree.root.collapse_all()


def main():
    cwd = sys.argv[1] if len(sys.argv) > 1 else str(Path.cwd())
    app = SessionTreeApp(cwd)
    app.run()


if __name__ == "__main__":
    main()
