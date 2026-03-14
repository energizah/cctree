#!/usr/bin/env python3
"""Extract and display the tree section from TUI SVG screenshots.

Usage: python dump_screenshots.py <message> [svg_files...]

Searches all screenshots (or specified files) for tree nodes matching
<message>, and displays focused context around matches.
"""

import html
import re
import sys
from pathlib import Path

SCREENSHOTS = Path("snap")
LOG = Path("cctree.log")


def extract_lines(svg_path: Path) -> tuple[str, list[str], int | None]:
    """Parse SVG, return (title, [lines], cursor_index).

    cursor_index is the index into lines of the highlighted/cursor row,
    or None if not detected.
    """
    text = svg_path.read_text()

    # Title from the special title class
    title_m = re.search(r'class="[^"]*-title"[^>]*>([^<]+)</text>', text)
    title = html.unescape(title_m.group(1)) if title_m else ""

    # Extract line numbers and their text spans
    # Each line is identified by clip-path="url(#...-line-N)"
    line_spans: dict[int, list[tuple[float, str]]] = {}
    for m in re.finditer(
        r'<text [^>]*x="([^"]+)"[^>]*clip-path="url\(#[^)]*-line-(\d+)\)"[^>]*>([^<]*)</text>',
        text,
    ):
        x = float(m.group(1))
        line_num = int(m.group(2))
        content = html.unescape(m.group(3)).replace("\xa0", " ")
        if content.strip():
            line_spans.setdefault(line_num, []).append((x, content))

    # Detect cursor line: find the widest rect with a non-standard fill color.
    # Standard bg: #1e1e1e, #121212, #003054, #292929, #000000, #242f38, etc.
    IGNORE_FILLS = {"#1e1e1e", "#121212", "#003054", "#292929", "#000000",
                    "#242f38", "#272727", "#0178d4", "#e0e0e0"}
    # Get SVG height to restrict to tree area (top 80%)
    vh_m = re.search(r'viewBox="[^"]*\s([\d.]+)"', text)
    max_y = float(vh_m.group(1)) * 0.8 if vh_m else 900
    highlight_y = None
    highlight_width = 0
    for m in re.finditer(
        r'<rect[^>]*fill="([^"]+)"[^>]*y="([^"]+)"[^>]*width="([^"]+)"[^>]*/>',
        text,
    ):
        fill, y, w = m.group(1), float(m.group(2)), float(m.group(3))
        if fill in IGNORE_FILLS or fill.startswith("rgba"):
            continue
        if y > max_y:
            continue  # skip input bar / status bar area
        if w > highlight_width:
            highlight_width = w
            highlight_y = y

    # Map highlight rect y -> line number via text y (text_y = rect_y + 18.5)
    cursor_line = None
    if highlight_y is not None and highlight_width > 500:
        target_text_y = highlight_y + 18.5
        # Find text line whose y is closest to target
        for line_num in sorted(line_spans):
            for tm in re.finditer(
                rf'<text [^>]*y="([^"]+)"[^>]*clip-path="url\(#[^)]*-line-{line_num}\)"',
                text,
            ):
                text_y = float(tm.group(1))
                if abs(text_y - target_text_y) < 5:
                    cursor_line = line_num
                break
            if cursor_line is not None:
                break

    # Assemble lines in order, spans sorted by x position
    lines = []
    line_num_to_idx = {}
    for n in sorted(line_spans):
        line_num_to_idx[n] = len(lines)
        spans = sorted(line_spans[n], key=lambda s: s[0])
        line = "".join(s[1] for s in spans)
        lines.append(line)

    cursor_idx = line_num_to_idx.get(cursor_line) if cursor_line is not None else None
    return title, lines, cursor_idx


def find_tree_section(lines: list[str], cursor_idx: int | None = None) -> list[str]:
    """Return just the tree widget lines, trimmed of chrome and blanks.

    The cursor line (if detected) is marked with ' ◄' suffix.
    """
    start = 0
    end = len(lines)

    # Tree starts at first line with tree drawing chars
    for i, line in enumerate(lines):
        if "▼" in line or "▶" in line or "├" in line:
            start = i
            break

    # Tree ends before the input bar or status bar
    for i in range(len(lines) - 1, start, -1):
        line = lines[i]
        if "sessions" in line.lower() and "│" in line:
            end = i + 1  # include status line
            break
        if "▔" in line or "▁" in line or "Send a message" in line:
            end = i
            break

    result = []
    idx = start
    for line in lines[start:end]:
        # Strip trailing detail-panel separator
        clean = line.rstrip("│").rstrip()
        # Skip blank/whitespace-only lines and scrollbar artifacts
        if not clean or clean in ("▉", "▊", "▎") or set(clean) <= {"│", " "}:
            idx += 1
            continue
        # Skip scrollbar-only lines
        if all(c in "▇▅▉▊▎│ " for c in clean):
            idx += 1
            continue
        if cursor_idx is not None and idx == cursor_idx:
            clean += "  ◄"
        result.append(clean)
        idx += 1
    return result


def _focus_around(
    lines: list[str],
    focus_terms: list[str],
    context: int = 3,
) -> list[str]:
    """Return lines near focus_terms with '...' elision for gaps."""
    if not lines:
        return []
    hits: set[int] = set()
    for i, line in enumerate(lines):
        low = line.lower()
        if any(t.lower() in low for t in focus_terms):
            for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                hits.add(j)

    if not hits:
        return lines  # no focus terms found, show all

    result = []
    prev = -2
    for i in sorted(hits):
        if i > prev + 1:
            result.append("  ...")
        result.append(lines[i])
        prev = i
    if prev < len(lines) - 1:
        result.append("  ...")
    return result


def get_timestamp(svg_name: str) -> str:
    """Find the log timestamp for this screenshot number."""
    target = f"{svg_name}.svg"
    try:
        for line in LOG.read_text().splitlines():
            if target in line:
                return line.split()[0]
    except FileNotFoundError:
        pass
    return "??:??:??.???"


def main():
    if len(sys.argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        sys.exit(1)

    message = sys.argv[1]
    focus_terms = [message, "⏳"]

    if len(sys.argv) > 2:
        files = [Path(f) for f in sys.argv[2:]]
    else:
        files = sorted(SCREENSHOTS.glob("*.svg"))

    # Two-pass: find which files match, then include N before the first match
    CONTEXT_BEFORE = 3
    matching = set()
    for i, svg_path in enumerate(files):
        title, lines, cursor_idx = extract_lines(svg_path)
        tree = find_tree_section(lines, cursor_idx)
        if any(message.lower() in line.lower() for line in tree):
            matching.add(i)

    if not matching:
        print(f"No screenshots contain '{message}'", file=sys.stderr)
        sys.exit(1)

    first_match = min(matching)
    show = set(matching)
    for i in range(max(0, first_match - CONTEXT_BEFORE), first_match):
        show.add(i)

    for i in sorted(show):
        svg_path = files[i]
        name = svg_path.stem
        title, lines, cursor_idx = extract_lines(svg_path)
        tree = find_tree_section(lines, cursor_idx)
        ts = get_timestamp(name)
        relpath = str(svg_path)

        is_before = i < first_match
        if is_before:
            # Show full tree for context before the message appears
            focus = tree
        else:
            focus = _focus_around(tree, focus_terms=focus_terms, context=2)
        focus = [l for l in focus if not l.startswith("▊") and "▔▔▔" not in l and "▁▁▁" not in l]

        label = "  (before)" if is_before else ""
        print(f"{'─' * 72}")
        print(f"{relpath}  [{ts}]  {title}{label}")
        print()
        for line in focus:
            print(f"  {line}")
        print()


if __name__ == "__main__":
    main()
