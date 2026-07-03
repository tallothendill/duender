#!/usr/bin/env python3
# Post-process gcode:
# 1) After each tool call Tn:
#    - Find the first *relevant* move (G0/G1), ignoring E-only extrusion moves (G1 E...).
#    - If that first relevant move is Z-only (G0/G1 Z... without X/Y/E),
#      then find the next XY-only move (G0/G1 with X or Y, without Z/E).
#      Swap those two move lines and transfer F from Z-move to XY-move (only if XY has no F).
# 2) After the first occurrence of ";LAYER_CHANGE" in the file:
#    - Replace ALL M109 with M104 (keep params; convert R->S if used), preserving comments.
#
# (M106 replacement removed)

import re
import sys
from pathlib import Path

TOOL_RE = re.compile(r'^\s*T(\d+)\s*(?:;.*)?$', re.IGNORECASE)

# Move commands: G0 or G1
MOVE_RE = re.compile(r'^\s*G0\b|^\s*G1\b', re.IGNORECASE)
G1_RE   = re.compile(r'^\s*G1\b', re.IGNORECASE)

M109_RE = re.compile(r'^\s*M109\b', re.IGNORECASE)

F_RE = re.compile(r'(?<![A-Z])F\s*([-+]?\d*\.?\d+)', re.IGNORECASE)

def strip_comment_keep(line: str):
    if ';' in line:
        a, b = line.split(';', 1)
        return a.rstrip(), ';' + b  # keep comment incl ';'
    return line.rstrip('\n'), ''

def strip_comment(line: str) -> str:
    return line.split(';', 1)[0].strip()

def has_axis(cmd: str, axis: str) -> bool:
    return re.search(rf'(?<![A-Z]){axis}\s*[-+]?\d*\.?\d+', cmd, re.IGNORECASE) is not None

def is_e_only_move(cmd: str) -> bool:
    # Only for G1 extrusion moves: G1 E... without X/Y/Z
    return (G1_RE.match(cmd)
            and has_axis(cmd, 'E')
            and not has_axis(cmd, 'X')
            and not has_axis(cmd, 'Y')
            and not has_axis(cmd, 'Z'))

def is_z_only_move(cmd: str) -> bool:
    # G0/G1 with Z, and without X/Y/E
    return (MOVE_RE.match(cmd)
            and has_axis(cmd, 'Z')
            and not has_axis(cmd, 'X')
            and not has_axis(cmd, 'Y')
            and not has_axis(cmd, 'E'))

def is_xy_only_move(cmd: str) -> bool:
    # G0/G1 with X or Y, and without Z/E
    return (MOVE_RE.match(cmd)
            and (has_axis(cmd, 'X') or has_axis(cmd, 'Y'))
            and not has_axis(cmd, 'Z')
            and not has_axis(cmd, 'E'))

def extract_F(code: str):
    m = F_RE.search(code)
    return m.group(1) if m else None

def remove_first_F(code: str) -> str:
    return re.sub(r'\s*(?<![A-Z])F\s*[-+]?\d*\.?\d+', '', code, count=1, flags=re.IGNORECASE).strip()

def add_F(code: str, fval: str) -> str:
    return f"{code.strip()} F{fval}"

def transfer_F(z_line: str, xy_line: str):
    """
    Move F from z_line to xy_line if:
    - z_line has F
    - xy_line has no F
    Preserve comments.
    """
    z_code, z_cmt = strip_comment_keep(z_line)
    xy_code, xy_cmt = strip_comment_keep(xy_line)

    zF = extract_F(z_code)
    xyF = extract_F(xy_code)

    if zF and not xyF:
        z_code2 = remove_first_F(z_code)
        xy_code2 = add_F(xy_code, zF)

        z_out = z_code2 + ((' ' + z_cmt.strip()) if z_cmt else '') + "\n"
        xy_out = xy_code2 + ((' ' + xy_cmt.strip()) if xy_cmt else '') + "\n"
        return z_out, xy_out

    return z_line, xy_line

def replace_m109_with_m104(line: str) -> str:
    # Convert wait-temp to set-temp, keep temp params; convert R->S
    code, cmt = strip_comment_keep(line)
    code2 = re.sub(r'^\s*M109\b', 'M104', code, flags=re.IGNORECASE)
    code2 = re.sub(r'(?<![A-Z])R\s*([-+]?\d*\.?\d+)', r'S\1', code2, count=1, flags=re.IGNORECASE)

    out = code2
    if cmt:
        out += ' ' + cmt.strip()
    return out + "\n"

def process(lines) -> bool:
    changed = False

    # --- Part A: global M109->M104 after first ;LAYER_CHANGE (independent) ---
    layer_idx = None
    for idx, line in enumerate(lines):
        if line.lstrip().startswith(";LAYER_CHANGE"):
            layer_idx = idx
            break

    if layer_idx is not None:
        for idx in range(layer_idx + 1, len(lines)):
            c = strip_comment(lines[idx])
            if M109_RE.match(c):
                new_line = replace_m109_with_m104(lines[idx])
                if new_line != lines[idx]:
                    lines[idx] = new_line
                    changed = True

    # --- Part B: swap Z-only and next XY-only after each Tn (independent) ---
    i = 0
    while i < len(lines):
        cmd_i = strip_comment(lines[i])
        if not TOOL_RE.match(cmd_i):
            i += 1
            continue

        z_idx = None
        xy_idx = None
        first_relevant_move_found = False

        j = i + 1
        while j < len(lines):
            c = strip_comment(lines[j])

            # Stop at next tool call
            if TOOL_RE.match(c):
                break

            # Ignore extrusion-only G1 E... while searching the first relevant move
            if not first_relevant_move_found and is_e_only_move(c):
                j += 1
                continue

            # Find the first relevant move (G0/G1) after T (excluding E-only G1)
            if not first_relevant_move_found and MOVE_RE.match(c):
                first_relevant_move_found = True
                if is_z_only_move(c):
                    z_idx = j
                else:
                    # First relevant move isn't Z-only -> do nothing for this tool block
                    z_idx = None
                    break
                j += 1
                continue

            # After Z-only, find next XY-only move
            if z_idx is not None and xy_idx is None and is_xy_only_move(c):
                xy_idx = j
                break

            j += 1

        if z_idx is None or xy_idx is None:
            i += 1
            continue

        # Transfer F from Z-move to XY-move (before swap)
        new_z, new_xy = transfer_F(lines[z_idx], lines[xy_idx])
        if new_z != lines[z_idx] or new_xy != lines[xy_idx]:
            lines[z_idx], lines[xy_idx] = new_z, new_xy
            changed = True

        # Swap the two move lines
        lines[z_idx], lines[xy_idx] = lines[xy_idx], lines[z_idx]
        changed = True

        # Continue after the swapped block
        i = xy_idx + 1

    return changed

def main():
    if len(sys.argv) < 2:
        raise SystemExit("Expected gcode filepath as last argument")

    gcode_path = Path(sys.argv[-1].strip('"'))
    if not gcode_path.is_file():
        raise SystemExit(f"Gcode file not found: {gcode_path}")

    lines = gcode_path.read_text(encoding="utf-8", errors="ignore").splitlines(True)
    changed = process(lines)
    if changed:
        gcode_path.write_text("".join(lines), encoding="utf-8")

if __name__ == "__main__":
    main()
