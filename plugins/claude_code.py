"""Claude Code backend plugin for Canvas Chat.

Registers REST/SSE endpoints that bridge Canvas Chat to the Claude Code CLI.
Loaded as an external plugin via config.yaml + importlib.
"""

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional
from uuid import uuid4

import anyio
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from canvas_chat.app import app

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    prompt: str
    session_id: Optional[str] = None
    cwd: Optional[str] = None


class ForkRequest(BaseModel):
    session_id: str
    cwd: Optional[str] = None


class ImportRequest(BaseModel):
    session_id: str
    cwd: Optional[str] = None
    max_tool_output: int = Field(default=2000, ge=0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CLAUDE_BIN: Optional[str] = shutil.which("claude")


def _claude_bin() -> str:
    if CLAUDE_BIN is None:
        raise HTTPException(status_code=503, detail="claude CLI not found on PATH")
    return CLAUDE_BIN


def _subprocess_env() -> dict[str, str]:
    """Build env for the claude subprocess, stripping CLAUDECODE to avoid nesting."""
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    return env


def _encode_cwd(cwd: str) -> str:
    """Encode a cwd path the same way Claude Code does for project directory names."""
    return re.sub(r"[^a-zA-Z0-9\-]", "-", cwd)


def _sessions_dir(cwd: Optional[str] = None) -> Path:
    """Return the JSONL sessions directory for a given cwd."""
    base = Path.home() / ".claude" / "projects"
    if cwd:
        return base / _encode_cwd(cwd)
    # Default: list all project dirs
    return base


def _find_session_file(session_id: str, cwd: Optional[str] = None) -> Optional[Path]:
    """Find a session JSONL file by session ID, searching under cwd or all projects."""
    base = _sessions_dir()
    if not base.exists():
        return None
    if cwd:
        # Prefix match: /home/adam matches -home-adam, -home-adam-src-foo, etc.
        encoded = _encode_cwd(cwd)
        dirs = [d for d in base.iterdir() if d.is_dir() and d.name.startswith(encoded)]
    else:
        dirs = [d for d in base.iterdir() if d.is_dir()]
    for project_dir in dirs:
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate
    return None


# ---------------------------------------------------------------------------
# POST /api/claude-code/chat — stream a Claude Code response via SSE
# ---------------------------------------------------------------------------


@app.post("/api/claude-code/chat")
async def claude_code_chat(req: ChatRequest, request: Request):
    """Stream Claude Code CLI output as SSE events."""
    claude = _claude_bin()

    cmd = [
        claude,
        "--print",
        "--verbose",
        "--output-format", "stream-json",
        "--dangerously-skip-permissions",
    ]
    if req.session_id:
        cmd.extend(["--resume", req.session_id])
    cmd.append(req.prompt)

    env = _subprocess_env()
    cwd = req.cwd or "/tmp"
    logger.info("Spawning: %s (cwd=%s)", " ".join(cmd), cwd)

    async def generate():
        try:
            process = await anyio.open_process(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=cwd,
            )
        except Exception as exc:
            yield {"event": "error", "data": f"Failed to start claude: {exc}"}
            return

        result_session_id = None
        cost_usd = None
        buf = b""

        try:
            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    process.kill()
                    return

                try:
                    chunk = await process.stdout.receive(4096)
                except anyio.EndOfStream:
                    break

                buf += chunk
                while b"\n" in buf:
                    raw_line, buf = buf.split(b"\n", 1)
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue

                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    etype = event.get("type")

                    match etype:
                        case "assistant":
                            # Full message object with content array
                            msg = event.get("message", {})
                            content = msg.get("content", [])
                            if isinstance(content, list):
                                for block in content:
                                    match block.get("type"):
                                        case "text":
                                            yield {"event": "message", "data": block.get("text", "")}
                                        case "tool_use":
                                            yield {
                                                "event": "status",
                                                "data": f"Using tool: {block.get('name', '')}",
                                            }
                                        case "tool_result":
                                            pass
                        case "result":
                            result_session_id = event.get("session_id")
                            cost_usd = event.get("total_cost_usd", event.get("cost_usd"))

            # Wait for process to finish
            await process.wait()

            # Check stderr for errors
            if process.returncode != 0:
                try:
                    stderr_data = await process.stderr.receive(4096)
                    stderr_text = stderr_data.decode("utf-8", errors="replace").strip()
                except anyio.EndOfStream:
                    stderr_text = ""
                if stderr_text:
                    logger.error("claude stderr: %s", stderr_text)
                    yield {"event": "error", "data": stderr_text}
                    return

            # Send done event with metadata
            done_data = {}
            if result_session_id:
                done_data["session_id"] = result_session_id
            if cost_usd is not None:
                done_data["cost_usd"] = cost_usd
            yield {"event": "done", "data": json.dumps(done_data) if done_data else ""}

        except Exception as exc:
            logger.exception("Error streaming claude output")
            yield {"event": "error", "data": str(exc)}
        finally:
            if process.returncode is None:
                process.kill()

    return EventSourceResponse(generate())


# ---------------------------------------------------------------------------
# POST /api/claude-code/fork — fork a session at its current tip
# ---------------------------------------------------------------------------


@app.post("/api/claude-code/fork")
async def claude_code_fork(req: ForkRequest):
    """Fork a Claude Code session, returning the new session ID."""
    claude = _claude_bin()

    cmd = [
        claude,
        "--print",
        "--resume", req.session_id,
        "--fork-session",
        "--output-format", "json",
        "--dangerously-skip-permissions",
        ".",
    ]

    cwd = req.cwd or "/tmp"

    try:
        result = await anyio.run_process(
            cmd,
            stdin=subprocess.DEVNULL,
            env=_subprocess_env(),
            cwd=cwd,
            check=False,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Fork failed: {exc}")

    stdout = result.stdout.decode("utf-8", errors="replace").strip()

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise HTTPException(
            status_code=500,
            detail=f"Fork failed (exit {result.returncode}): {stderr or stdout}",
        )

    # Parse the JSON output to get the session_id
    try:
        data = json.loads(stdout)
        fork_id = data.get("session_id")
    except (json.JSONDecodeError, AttributeError):
        # Try to find session ID in multi-line output
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if "session_id" in data:
                    fork_id = data["session_id"]
                    break
            except json.JSONDecodeError:
                continue
        else:
            raise HTTPException(
                status_code=500,
                detail=f"Could not parse fork session ID from output: {stdout[:500]}",
            )

    return {"fork_session_id": fork_id}


# ---------------------------------------------------------------------------
# POST /api/claude-code/import — parse JSONL session into canvas nodes/edges
# ---------------------------------------------------------------------------


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"\n\n... (truncated, {len(text)} chars total)"


def _extract_text_content(message: dict) -> str:
    """Extract displayable text from a message's content field."""
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


@app.post("/api/claude-code/import")
async def claude_code_import(req: ImportRequest):
    """Parse a Claude Code JSONL session into canvas nodes and edges."""
    session_file = _find_session_file(req.session_id, req.cwd)
    if session_file is None:
        raise HTTPException(status_code=404, detail=f"Session {req.session_id} not found")

    # Parse all conversation records
    records: list[dict] = []
    record_by_uuid: dict[str, dict] = {}

    async with await anyio.open_file(session_file, "r") as f:
        async for line in f:
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

    # Coalesce consecutive assistant records:
    # If an assistant record's parentUuid points to another assistant record,
    # merge it into the parent (it's part of the same turn).
    coalesced: dict[str, dict] = {}  # uuid -> merged record
    merge_target: dict[str, str] = {}  # uuid -> root assistant uuid

    for rec in records:
        rtype = rec.get("type")
        uuid = rec.get("uuid")
        parent_uuid = rec.get("parentUuid")

        if rtype == "assistant" and parent_uuid and parent_uuid in record_by_uuid:
            parent_rec = record_by_uuid[parent_uuid]
            if parent_rec.get("type") == "assistant":
                # Find the root of this assistant chain
                root = parent_uuid
                while root in merge_target:
                    root = merge_target[root]
                merge_target[uuid] = root

                # Merge content into root
                if root in coalesced:
                    root_msg = coalesced[root]["message"]
                else:
                    coalesced[root] = {**record_by_uuid[root]}
                    root_msg = coalesced[root]["message"]

                # Append content blocks
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
                coalesced[uuid] = rec

    # Build nodes and edges
    nodes = []
    edges = []
    uuid_to_node_id: dict[str, str] = {}
    children_count: dict[str, int] = {}  # parent_uuid -> child count

    # Count children for branching detection
    for uuid, rec in coalesced.items():
        parent_uuid = rec.get("parentUuid")
        if parent_uuid:
            # Resolve through merge targets
            effective_parent = parent_uuid
            while effective_parent in merge_target:
                effective_parent = merge_target[effective_parent]
            children_count[effective_parent] = children_count.get(effective_parent, 0) + 1

    # Assign positions via DFS
    NODE_WIDTH = 400
    NODE_GAP_X = 100
    NODE_GAP_Y = 40
    y_cursor = 0

    # Build adjacency list
    children_of: dict[str, list[str]] = {}
    root_uuids: list[str] = []

    for uuid, rec in coalesced.items():
        parent_uuid = rec.get("parentUuid")
        effective_parent = parent_uuid
        if effective_parent:
            while effective_parent in merge_target:
                effective_parent = merge_target[effective_parent]

        if effective_parent and effective_parent in coalesced:
            children_of.setdefault(effective_parent, []).append(uuid)
        elif not effective_parent or effective_parent not in coalesced:
            root_uuids.append(uuid)

    # Sort children by timestamp
    for parent, kids in children_of.items():
        kids.sort(key=lambda u: coalesced[u].get("timestamp", ""))

    # DFS to assign positions
    def dfs(uuid: str, depth: int):
        nonlocal y_cursor
        rec = coalesced[uuid]
        rtype = rec.get("type")
        message = rec.get("message", {})

        content = _extract_text_content(message)
        if rtype == "assistant" and req.max_tool_output > 0:
            # Truncate tool outputs within the content
            content = _truncate(content, req.max_tool_output * 10)

        node_id = str(uuid4())
        uuid_to_node_id[uuid] = node_id

        node_type = "human" if rtype == "user" else "ai"
        model = message.get("model")
        timestamp = rec.get("timestamp", "")

        x = depth * (NODE_WIDTH + NODE_GAP_X)
        y = y_cursor

        # Estimate height from content
        lines = max(3, min(20, len(content) // 60 + 1))
        height = lines * 24 + 60

        nodes.append({
            "id": node_id,
            "type": node_type,
            "content": content,
            "position": {"x": x, "y": y},
            "width": NODE_WIDTH,
            "height": height,
            "created_at": timestamp,
            "model": model,
            "claude_uuid": uuid,
            "session_id": req.session_id,
        })

        y_cursor += height + NODE_GAP_Y

        # Create edge from parent
        parent_uuid = rec.get("parentUuid")
        effective_parent = parent_uuid
        if effective_parent:
            while effective_parent in merge_target:
                effective_parent = merge_target[effective_parent]

        if effective_parent and effective_parent in uuid_to_node_id:
            parent_children = children_count.get(effective_parent, 1)
            edge_type = "branch" if parent_children > 1 else "reply"
            edges.append({
                "id": str(uuid4()),
                "source": uuid_to_node_id[effective_parent],
                "target": node_id,
                "type": edge_type,
            })

        # Recurse into children
        for child_uuid in children_of.get(uuid, []):
            dfs(child_uuid, depth + 1)

    for root_uuid in root_uuids:
        dfs(root_uuid, 0)

    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# POST /api/claude-code/import-dag — unified DAG from all sessions in a cwd
# ---------------------------------------------------------------------------


def _parse_session_file(path: Path) -> list[dict]:
    """Parse a JSONL session file into coalesced message records.

    Returns a list of dicts with keys: type, message, timestamp, content_hash.
    Consecutive assistant records are merged. The content_hash is computed from
    the raw message content for fork detection.
    """
    import hashlib

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

    # Coalesce consecutive assistant records
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

                # Find or create root entry in coalesced list
                root_entry = None
                for entry in coalesced:
                    if entry.get("_root_uuid") == root:
                        root_entry = entry
                        break
                if root_entry is None:
                    root_rec = record_by_uuid[root]
                    root_entry = {**root_rec, "_root_uuid": root}
                    # Replace existing entry
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

    # Compute content hashes for fork detection
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


class ImportDagRequest(BaseModel):
    cwd: str
    max_tool_output: int = Field(default=2000, ge=0)
    format: str = Field(default="tree", pattern="^(tree|nodes)$")


@app.post("/api/claude-code/import-dag")
async def claude_code_import_dag(req: ImportDagRequest):
    """Build a unified DAG from all sessions under a cwd by detecting forks.

    Sessions that share the same message prefix are merged: the shared part
    becomes a single trunk, and divergence points become branches.
    """
    base = _sessions_dir()
    if not base.exists():
        return {"nodes": [], "edges": []}

    encoded = _encode_cwd(req.cwd)
    dirs = [d for d in base.iterdir() if d.is_dir() and d.name.startswith(encoded)]

    # Parse all sessions
    all_sessions: list[list[dict]] = []
    for d in dirs:
        for jsonl_file in d.glob("*.jsonl"):
            msgs = _parse_session_file(jsonl_file)
            if msgs:
                all_sessions.append(msgs)

    if not all_sessions:
        return {"nodes": [], "edges": []}

    # Sort sessions longest first — the longest is most likely the "trunk"
    all_sessions.sort(key=len, reverse=True)

    # Build a trie keyed by content_hash sequences.
    # Each trie node: { children: {hash: node}, messages: [msg_variants], count: int }
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

    # -- Tree format: compact text rendering of the DAG --
    if req.format == "tree":
        lines: list[str] = []

        def _preview(msg: dict) -> str:
            """Extract a one-line preview from a message."""
            content = msg["message"].get("content", "")
            if isinstance(content, str):
                preview = content
            else:
                preview = ""
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        preview = block.get("text", "")
                        break
                if not preview:
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            preview = f"[{block.get('name', 'tool')}]"
                            break
            preview = preview.replace("\n", " ").strip()
            return preview[:57] + "..." if len(preview) > 60 else preview

        # Iterative DFS: stack of (trie_node, prefix)
        # We push children in reverse so first child is processed first.
        # Each stack entry produces lines for the children of trie_node.
        render_stack: list[tuple[dict, str]] = [(trie_root, "")]

        while render_stack:
            trie_node, prefix = render_stack.pop()
            children_items = list(trie_node["children"].items())

            # Push children in reverse (with their computed lines/prefix)
            # so they come off the stack in forward order.
            entries = []
            for idx, (h, child) in enumerate(children_items):
                last = idx == len(children_items) - 1

                # Collect linear chain
                chain = [child]
                cur = child
                while len(cur["children"]) == 1:
                    cur = next(iter(cur["children"].values()))
                    chain.append(cur)

                msg = chain[0]["messages"][0]
                preview = _preview(msg)
                connector = "└─" if last else "├─"
                count = chain[0]["count"]
                n_msgs = len(chain)

                if n_msgs == 1:
                    role = "H" if msg["type"] == "user" else "A"
                    line = f"{prefix}{connector} {role}: {preview}"
                else:
                    line = f"{prefix}{connector} [{n_msgs} msgs] {preview}"

                if count > 1:
                    line += f"  ×{count}"

                end_node = chain[-1]
                child_prefix = prefix + ("   " if last else "│  ")
                entries.append((line, end_node, child_prefix))

            # Push in reverse so first entry is popped first
            for line, end_node, child_prefix in reversed(entries):
                if end_node["children"]:
                    render_stack.append((end_node, child_prefix))
                # Prepend a marker so we can insert the line in order
                lines.append((line, len(render_stack)))

        # The lines list has (line_text, _) tuples; extract just text
        # But the ordering is wrong because of stack reversal.
        # Let me redo this properly.
        lines.clear()

        # Simpler iterative approach: use a stack that yields lines in order.
        # Stack entries: ("line", line_text) or ("visit", trie_node, prefix)
        render_stack2: list[tuple] = [("visit", trie_root, "")]

        while render_stack2:
            entry = render_stack2.pop()
            if entry[0] == "line":
                lines.append(entry[1])
                continue

            _, trie_node, prefix = entry
            children_items = list(trie_node["children"].items())

            # Push in reverse so first child's line comes first when popped
            for idx in range(len(children_items) - 1, -1, -1):
                h, child = children_items[idx]
                last = idx == len(children_items) - 1

                chain = [child]
                cur = child
                while len(cur["children"]) == 1:
                    cur = next(iter(cur["children"].values()))
                    chain.append(cur)

                msg = chain[0]["messages"][0]
                preview = _preview(msg)
                connector = "└─" if last else "├─"
                count = chain[0]["count"]
                n_msgs = len(chain)

                if n_msgs == 1:
                    role = "H" if msg["type"] == "user" else "A"
                    line = f"{prefix}{connector} {role}: {preview}"
                else:
                    line = f"{prefix}{connector} [{n_msgs} msgs] {preview}"

                if count > 1:
                    line += f"  ×{count}"

                end_node = chain[-1]
                child_prefix = prefix + ("   " if last else "│  ")

                # Push visit first (will be processed after the line)
                if end_node["children"]:
                    render_stack2.append(("visit", end_node, child_prefix))
                render_stack2.append(("line", line))

        tree_text = "\n".join(lines)

        return {
            "tree": tree_text,
            "session_count": len(all_sessions),
            "node_count": sum(len(s) for s in all_sessions),
        }

    # -- Nodes format: canvas nodes/edges with chain collapsing --

    # Collapse linear chains in the trie.
    # Walk the trie and replace single-child runs with a summary.
    # A "segment" is a list of consecutive trie nodes each with exactly 1 child.
    # At branch points (>1 child) or leaves (0 children) the segment ends.

    def _collect_segment(start_node: dict) -> tuple[list[dict], dict]:
        """Follow single-child chain from start_node, returning (chain, end_node).

        chain contains the trie nodes in the run (including start_node).
        end_node is the last node in the chain (which has 0 or >1 children).
        """
        chain = [start_node]
        current = start_node
        while len(current["children"]) == 1:
            child = next(iter(current["children"].values()))
            chain.append(child)
            current = child
        return chain, current

    def _segment_content(chain: list[dict], max_tool_output: int) -> str:
        """Build display content for a collapsed segment."""
        if len(chain) == 1:
            msg = chain[0]["messages"][0]
            content = _extract_text_content(msg["message"])
            if msg["type"] == "assistant" and max_tool_output > 0:
                content = _truncate(content, max_tool_output * 10)
            return content

        first_msg = chain[0]["messages"][0]
        last_msg = chain[-1]["messages"][0]
        first_text = _extract_text_content(first_msg["message"])
        last_text = _extract_text_content(last_msg["message"])

        # Count user and assistant messages in the chain
        n_user = sum(1 for n in chain if n["messages"][0]["type"] == "user")
        n_asst = sum(1 for n in chain if n["messages"][0]["type"] == "assistant")

        first_preview = _truncate(first_text, 200)
        last_preview = _truncate(last_text, 200)

        parts = [
            f"**{len(chain)} messages** ({n_user} human, {n_asst} AI)",
            "",
            f"**First:** {first_preview}",
            "",
            "---",
            "",
            f"**Last:** {last_preview}",
        ]
        return "\n".join(parts)

    # Convert collapsed trie to canvas nodes/edges via iterative DFS
    NODE_WIDTH = 400
    NODE_GAP_X = 100
    NODE_GAP_Y = 40
    y_cursor = 0

    nodes = []
    edges = []

    # Stack: (trie_node, parent_canvas_id, depth, n_siblings)
    stack: list[tuple[dict, str | None, int, int]] = []

    # Seed stack with root's children (reversed for DFS order)
    n_root_children = len(trie_root["children"])
    for child in reversed(list(trie_root["children"].values())):
        stack.append((child, None, 0, n_root_children))

    while stack:
        trie_node, parent_canvas_id, depth, n_siblings = stack.pop()

        # Collect the linear segment starting at this node
        chain, end_node = _collect_segment(trie_node)

        # Build the canvas node for this segment
        content = _segment_content(chain, req.max_tool_output)
        first_msg = chain[0]["messages"][0]

        node_id = str(uuid4())
        # Use the type of the first message in the segment
        node_type = "human" if first_msg["type"] == "user" else "ai"
        model = first_msg["message"].get("model")
        timestamp = first_msg["timestamp"]

        # Collect session IDs across the segment
        all_session_ids: set[str] = set()
        for seg_node in chain:
            all_session_ids.update(seg_node["session_ids"])
        session_ids = list(all_session_ids)
        session_count = chain[0]["count"]

        x = depth * (NODE_WIDTH + NODE_GAP_X)
        y = y_cursor

        lines = max(3, min(20, len(content) // 60 + 1))
        height = lines * 24 + 60

        node_data = {
            "id": node_id,
            "type": "note" if len(chain) > 1 else node_type,
            "content": content,
            "position": {"x": x, "y": y},
            "width": NODE_WIDTH,
            "height": height,
            "created_at": timestamp,
            "model": model,
            "session_id": session_ids[0] if session_ids else None,
            "session_count": session_count,
            "collapsed_count": len(chain),
        }

        # Include individual messages for collapsed segments so frontend can expand
        if len(chain) > 1:
            collapsed_msgs = []
            for seg_node in chain:
                seg_msg = seg_node["messages"][0]
                seg_content = _extract_text_content(seg_msg["message"])
                if seg_msg["type"] == "assistant" and req.max_tool_output > 0:
                    seg_content = _truncate(seg_content, req.max_tool_output * 10)
                collapsed_msgs.append({
                    "type": "human" if seg_msg["type"] == "user" else "ai",
                    "content": seg_content,
                    "timestamp": seg_msg["timestamp"],
                    "model": seg_msg["message"].get("model"),
                    "session_id": seg_msg["session_id"],
                })
            node_data["collapsed_messages"] = collapsed_msgs

        nodes.append(node_data)

        y_cursor += height + NODE_GAP_Y

        if parent_canvas_id:
            edge_type = "branch" if n_siblings > 1 else "reply"
            edges.append({
                "id": str(uuid4()),
                "source": parent_canvas_id,
                "target": node_id,
                "type": edge_type,
            })

        # Push the end_node's children onto the stack (these are branch points)
        n_end_children = len(end_node["children"])
        for child in reversed(list(end_node["children"].values())):
            stack.append((child, node_id, depth + 1, n_end_children))

    return {
        "nodes": nodes,
        "edges": edges,
        "session_count": len(all_sessions),
    }


# ---------------------------------------------------------------------------
# GET /api/claude-code/sessions — list available sessions
# ---------------------------------------------------------------------------


@app.get("/api/claude-code/sessions")
async def claude_code_sessions(cwd: Optional[str] = None):
    """List Claude Code sessions, optionally filtered by cwd."""
    results = []
    base = _sessions_dir()
    if not base.exists():
        return results

    dirs_to_scan = []
    if cwd:
        # Prefix match: /home/adam matches -home-adam, -home-adam-src-foo, etc.
        encoded = _encode_cwd(cwd)
        for d in base.iterdir():
            if d.is_dir() and d.name.startswith(encoded):
                dirs_to_scan.append(d)
    else:
        dirs_to_scan = [d for d in base.iterdir() if d.is_dir()]

    for project_dir in dirs_to_scan:
        for jsonl_file in project_dir.glob("*.jsonl"):
            session_id = jsonl_file.stem
            first_prompt = None
            message_count = 0
            timestamp = None

            try:
                with open(jsonl_file, "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        rtype = rec.get("type")
                        if rtype in ("user", "assistant"):
                            message_count += 1
                            if rtype == "user" and first_prompt is None:
                                msg = rec.get("message", {})
                                content = msg.get("content", "")
                                if isinstance(content, list):
                                    content = " ".join(
                                        b.get("text", "")
                                        for b in content
                                        if isinstance(b, dict) and b.get("type") == "text"
                                    )
                                # Strip system-injected XML tags
                                content = re.sub(r"<[^>]+>.*?</[^>]+>", "", content, flags=re.DOTALL).strip()
                                first_prompt = content[:200]
                            if timestamp is None:
                                timestamp = rec.get("timestamp")
            except Exception:
                continue

            if message_count > 0:
                results.append({
                    "session_id": session_id,
                    "first_prompt": first_prompt,
                    "timestamp": timestamp,
                    "message_count": message_count,
                    "project_dir": project_dir.name,
                })

    # Sort by timestamp descending
    results.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return results


logger.info("Claude Code plugin loaded: 4 endpoints registered")
