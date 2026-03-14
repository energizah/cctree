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


def _sessions_dir(cwd: Optional[str] = None) -> Path:
    """Return the JSONL sessions directory for a given cwd."""
    base = Path.home() / ".claude" / "projects"
    if cwd:
        encoded = cwd.replace("/", "-")
        return base / encoded
    # Default: list all project dirs
    return base


def _find_session_file(session_id: str, cwd: Optional[str] = None) -> Optional[Path]:
    """Find a session JSONL file by session ID, searching under cwd or all projects."""
    if cwd:
        candidate = _sessions_dir(cwd) / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate
        return None
    # Search all project dirs
    base = _sessions_dir()
    if not base.exists():
        return None
    for project_dir in base.iterdir():
        if not project_dir.is_dir():
            continue
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
        d = _sessions_dir(cwd)
        if d.exists():
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
