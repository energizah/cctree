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
import re
import sys
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Footer, Header, Static, Tree
from textual import work


# ---------------------------------------------------------------------------
# Pure helpers (copied from plugins/claude_code.py to avoid importing FastAPI)
# ---------------------------------------------------------------------------


def _encode_cwd(cwd: str) -> str:
    return re.sub(r"[^a-zA-Z0-9\-]", "-", cwd)


def _sessions_dir() -> Path:
    return Path.home() / ".claude" / "projects"


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
            node["session_ids"].add(msg["session_id"])

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
        dock: bottom;
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
    }
    #main {
        layout: horizontal;
        height: 1fr;
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

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            yield Tree("Sessions", id="tree")
            yield Static("Select a node to view details", id="detail")
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

    @work(thread=True)
    def load_tree(self) -> None:
        """Parse sessions in a worker thread so the UI stays responsive."""
        self.trie_root, self.session_count = _build_trie(self.cwd)
        self.call_from_thread(self._render_tree)

    def _render_tree(self) -> None:
        tree = self.query_one("#tree", Tree)
        tree.root.remove_children()

        self._add_trie_children(tree.root, self.trie_root)

        status = self.query_one("#status", Static)
        node_count = self._count_nodes(self.trie_root)
        status.update(f" {self.session_count} sessions, {node_count} messages")

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

            label = Text()
            if n_msgs == 1:
                role, role_style = _msg_role(msg)
                label.append(f"{role}: ", style=role_style)
                label.append(preview_text)
            else:
                label.append(f"[{n_msgs} msgs] ", style="bold yellow")
                label.append(preview_text)

            if count > 1:
                label.append(f"  \u00d7{count}", style="dim")

            chain_session_ids: set[str] = set()
            for seg in chain:
                chain_session_ids.update(seg["session_ids"])
            chain_session_ids.discard("")

            end_node = chain[-1]
            has_children = bool(end_node["children"])

            data = {
                "session_ids": sorted(chain_session_ids),
                "first_msg": chain[0]["messages"][0],
                "last_msg": chain[-1]["messages"][0],
                "msg_count": n_msgs,
                "count": count,
                "chain": chain if n_msgs > 1 else None,
                "_trie_node": end_node if has_children else None,
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
        self.log.info(f"expanded: node.data={event.node.data}")
        node = event.node
        data = self._get(node.data)
        if not data:
            return

        # Check if children are still just the placeholder
        children = list(node.children)
        if len(children) != 1 or children[0].data != -1:
            return

        # Remove placeholder
        children[0].remove()

        # Expand collapsed chain into individual messages
        chain = data.get("chain")
        if chain:
            for seg in chain:
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
                nid = self._store(msg_data)
                node.add_leaf(msg_label, data=nid)
            data["chain"] = None

        # Lazily add trie branch children
        trie_node = data.get("_trie_node")
        if trie_node:
            self._add_trie_children(node, trie_node)
            data["_trie_node"] = None

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

        first_msg = data.get("first_msg")
        if first_msg:
            result.append_text(_format_detail(first_msg))

        if msg_count > 1:
            last_msg = data.get("last_msg")
            if last_msg:
                result.append("\n\n")
                result.append_text(_format_detail(last_msg))

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
        first_msg = data.get("first_msg")
        if not first_msg:
            return
        result = _format_detail(first_msg)
        if data.get("msg_count", 1) > 1:
            last_msg = data.get("last_msg")
            if last_msg:
                result.append("\n\n")
                result.append_text(_format_detail(last_msg))
        self.copy_to_clipboard(result.plain)
        self.notify("Copied to clipboard")

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
