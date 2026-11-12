"""Shared helpers for the web-uat-companion scripts (Python 3.9+, stdlib + openpyxl)."""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

CONFIG_REL = Path("config") / "uat.config.json"


def die(msg, code=1):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(code)


def find_workspace(explicit=None):
    """Workspace = folder containing config/uat.config.json.
    Order: --ws arg, $UAT_WORKSPACE, cwd and its parents, ./uat-workspace, ./UAT-Automation/uat-workspace."""
    cands = []
    if explicit:
        cands.append(Path(explicit))
    if os.environ.get("UAT_WORKSPACE"):
        cands.append(Path(os.environ["UAT_WORKSPACE"]))
    cwd = Path.cwd()
    cands += [cwd, *cwd.parents]
    cands += [cwd / "uat-workspace", cwd / "UAT-Automation" / "uat-workspace"]
    for c in cands:
        if (c / CONFIG_REL).is_file():
            return c.resolve()
    die("Could not find a UAT workspace (a folder with config/uat.config.json). "
        "Pass --ws <path> or set UAT_WORKSPACE.")


def load_json(path, default=None):
    p = Path(path)
    if not p.exists():
        if default is not None:
            return default
        die("Missing file: %s" % p)
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    tmp.replace(p)


def load_config(ws):
    return load_json(Path(ws) / CONFIG_REL)


def workbook_path(ws, cfg):
    p = Path(cfg["source_workbook"])
    if not p.is_absolute():
        p = (Path(ws) / p).resolve()
    if not p.exists():
        die("Source workbook not found: %s (set source_workbook in config/uat.config.json)" % p)
    return p


def now_iso():
    return datetime.now().replace(microsecond=0).isoformat()


def clean(v):
    """Normalise a cell value to a stripped string ('' for empty / broken formulas)."""
    if v is None:
        return ""
    s = str(v).strip()
    if s.startswith("=") or "#REF!" in s:
        return ""
    return re.sub(r"[ \t]+", " ", s)


def script_num(script_id):
    """First number in the ID: Test-Script-009 -> 9, UAT2-001-02 -> 1."""
    m = re.search(r"(\d+)", re.sub(r"^UAT2-", "", script_id or "", flags=re.I))
    return int(m.group(1)) if m else -1


def script_key(script_id):
    """Sort key covering sub-IDs: UAT2-001-02 -> (1, 2); Test-Script-009 -> (9,)."""
    return tuple(int(x) for x in re.findall(r"\d+", re.sub(r"^UAT2-", "", script_id or "", flags=re.I))) or (-1,)


def script_label(script_id):
    """Jira label for a script ID."""
    if re.match(r"^Test-Script-", script_id or "", re.I):
        return "Test-Script-%03d" % script_num(script_id)
    return re.sub(r"[^A-Za-z0-9_-]", "-", script_id or "")


def expand_selection(sel, all_ids):
    """Select IDs present in all_ids (order preserved).
    '001-016,020'      -> by test-case number (Test-Script-001..016, or UAT2-001-* .. UAT2-016-*)
    'UAT2-001-02'      -> that exact sub test case;  'UAT2-024' -> every sub ID of UAT2-024
    'all'              -> everything"""
    if not sel or sel.strip().lower() == "all":
        return list(all_ids)
    wanted, exact = set(), set()
    for part in re.split(r"[,\s]+", sel.strip()):
        if not part:
            continue
        if re.match(r"^UAT2-\d+(-\d+)?$", part, re.I):
            exact.add(part.upper())
            continue
        m = re.match(r"^(?:Test-Script-)?(\d+)\s*(?:-|\.\.)\s*(?:Test-Script-)?(\d+)$", part, re.I)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            wanted.update(range(min(a, b), max(a, b) + 1))
        else:
            n = script_num(part)
            if n < 0:
                die("Cannot understand script selection: %r" % part)
            wanted.add(n)

    def hit(i):
        u = i.upper()
        return (script_num(i) in wanted or u in exact or any(u.startswith(e + "-") for e in exact))
    return [i for i in all_ids if hit(i)]
