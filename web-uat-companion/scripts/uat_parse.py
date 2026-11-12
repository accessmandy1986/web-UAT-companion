#!/usr/bin/env python3
"""Read the UAT workbook and turn every test script into structured JSON.

Usage:
  python3 uat_parse.py [--ws WORKSPACE] [--workbook PATH]

Writes:
  <ws>/tests/tests.json          all scripts (+ source row numbers for write-back)
  <ws>/tests/<Test-Script-NNN>.json
  <ws>/tests/parse_report.md     warnings, inferred IDs, ambiguous expected results
"""
import argparse
import re
from pathlib import Path

try:
    import openpyxl
except ImportError:  # pragma: no cover
    raise SystemExit("openpyxl is required: pip3 install --user openpyxl")

from uat_common import (clean, die, find_workspace, load_config, now_iso, save_json,
                        script_key, script_num, workbook_path)

ID_RE = re.compile(r"Test-Script-\d+", re.I)

# Words that make an expected result hard to verify objectively.
VAGUE_WORDS = ["applicable", "appropriate", "as expected", "correct", "correctly", "properly",
               "relevant", "accordingly", "if applicable", "as designed", "where applicable",
               "consistent", "acceptable"]
# Step hints that need a human or special handling.
HINTS = {
    "payment": r"\b(payment|pay now|card number|credit card|cvv|purchase|checkout and payment)\b",
    "otp_or_email_verification": r"\b(otp|one[- ]time|verification code|verify (your|the) email|email verification|inbox|activation (email|link))\b",
    "social_login": r"\b(google|apple id|sign in with apple|facebook|line account|wechat|social account)\b",
    "biometric": r"\b(biometric|face ?id|touch ?id|fingerprint)\b",
    "captcha": r"\b(h?captcha|recaptcha)\b",
    "mentions_login": r"\b(logged[- ]in|log in|login|sign in|member account|registered (test )?account|valid credentials)\b",
    "native_app": r"\b(ios|android|app store|play store|push notification|webview closes)\b",
}
TIERS = ["silver", "gold", "platinum", "titanium", "red"]

GROUPS = [("Desktop", "Expected Result (Desktop)"),
          ("Webview", "Expected Result (Webview)"),
          ("iOS", "Expected Result (App - iOS)"),
          ("Android", "Expected Result (App - Android)")]
GROUP_FIELDS = ["actual", "pass_fail", "defect_id", "tester", "test_date", "remarks"]


def norm_header(v):
    return re.sub(r"\s+", " ", str(v or "")).strip().lower()


def read_plan(ws):
    hdr_row, cols = None, {}
    for r in range(1, 10):
        vals = [norm_header(ws.cell(r, c).value) for c in range(1, ws.max_column + 1)]
        if "test script id" in vals:
            hdr_row = r
            for i, v in enumerate(vals, start=1):
                if v and v not in cols:
                    cols[v] = i
            break
    if not hdr_row:
        die("Plan sheet: could not find a 'Test Script ID' header row")
    # 'Applicable To' spans three sub-columns labelled on the next row (Web / Web Resp / App)
    sub = {}
    for c in range(1, ws.max_column + 1):
        v = norm_header(ws.cell(hdr_row + 1, c).value)
        if v in ("web", "web resp", "app"):
            sub[v] = c

    def col(name):
        return cols.get(name)

    plan = {}
    order = []
    for r in range(hdr_row + 1, ws.max_row + 1):
        sid = clean(ws.cell(r, col("test script id")).value)
        if not ID_RE.fullmatch(sid or ""):
            continue
        sid = "Test-Script-%03d" % script_num(sid)
        g = lambda name: clean(ws.cell(r, col(name)).value) if col(name) else ""
        plan[sid] = {
            "row": r,
            "journey": g("journey"),
            "sub_journey": g("sub-journey"),
            "section": g("section"),
            "test_case": g("test case"),
            "scenario": g("test scenario"),
            "applicable": {
                "Web": clean(ws.cell(r, sub["web"]).value).upper() == "Y" if "web" in sub else True,
                "Web Resp": clean(ws.cell(r, sub["web resp"]).value).upper() == "Y" if "web resp" in sub else True,
                "App": clean(ws.cell(r, sub["app"]).value).upper() == "Y" if "app" in sub else False,
            },
            "plan_columns": {k: cols.get(k) for k in ("actual start date", "actual end date", "actual tester", "status")},
            "status": g("status"),
        }
        order.append(sid)
    return plan, order


def header_map(ws, r):
    """Map the per-platform column groups from a block's 'Step' header row."""
    hdr = {c: norm_header(ws.cell(r, c).value) for c in range(1, ws.max_column + 1)}
    m = {"step": None, "description": None, "precondition": None, "groups": {}}
    for c, v in hdr.items():
        if v == "step":
            m["step"] = c
        elif v == "step description":
            m["description"] = c
        elif v.startswith("pre-test") or v.startswith("pre test") or v.startswith("precondition"):
            m["precondition"] = c
    for gname, label in GROUPS:
        exp = next((c for c, v in hdr.items() if v == label.lower()), None)
        if exp:
            m["groups"][gname] = {"expected": exp}
            for i, f in enumerate(GROUP_FIELDS, start=1):
                m["groups"][gname][f] = exp + i
    if not (m["step"] and m["description"] and "Desktop" in m["groups"]):
        return None
    return m


def flags_for(text):
    t = text.lower()
    found = [k for k, rx in HINTS.items() if re.search(rx, t)]
    tiers = [x for x in TIERS if re.search(r"\b%s\b" % x, t)]
    return found, tiers


def parse(wb_path, cfg):
    wb = openpyxl.load_workbook(wb_path, data_only=False)
    plan_ws = wb[cfg["sheets"]["plan"]]
    scr_ws = wb[cfg["sheets"]["scripts"]]
    plan, plan_order = read_plan(plan_ws)

    starts = [r for r in range(1, scr_ws.max_row + 1)
              if clean(scr_ws.cell(r, 1).value).lower().startswith("test script id")]
    warnings = []
    blocks = []
    for bi, r0 in enumerate(starts):
        r_end = (starts[bi + 1] - 1) if bi + 1 < len(starts) else scr_ws.max_row
        a1, b1 = clean(scr_ws.cell(r0, 1).value), clean(scr_ws.cell(r0, 2).value)
        explicit = None
        if ID_RE.search(b1):           # column B wins (column A sometimes holds a stale ID)
            explicit = ID_RE.search(b1).group(0)
        elif ID_RE.search(a1):
            explicit = ID_RE.search(a1).group(0)
        if explicit:
            explicit = "Test-Script-%03d" % script_num(explicit)

        def labelled(row, label):
            a = clean(scr_ws.cell(row, 1).value)
            b = clean(scr_ws.cell(row, 2).value)
            if not a.lower().startswith(label):
                return ""
            if b:
                return b
            return a.split(":", 1)[1].strip() if ":" in a else ""

        scenario = labelled(r0 + 1, "test scenario")
        component = labelled(r0 + 2, "test component")

        hdr_row = next((r for r in range(r0, min(r0 + 8, r_end + 1))
                        if norm_header(scr_ws.cell(r, 1).value) == "step"), None)
        hm = header_map(scr_ws, hdr_row) if hdr_row else None
        if not hm:
            warnings.append("Block at row %d: no usable 'Step' header row - skipped" % r0)
            continue
        steps = []
        for r in range(hdr_row + 1, r_end + 1):
            step_no = clean(scr_ws.cell(r, hm["step"]).value)
            desc = clean(scr_ws.cell(r, hm["description"]).value)
            if not step_no and not desc:
                continue
            if step_no.lower() == "step number":   # legend row
                continue
            if isinstance(scr_ws.cell(r, hm["step"]).value, float):
                step_no = str(int(scr_ws.cell(r, hm["step"]).value))
            pre = clean(scr_ws.cell(r, hm["precondition"]).value) if hm["precondition"] else ""
            expected = {g: clean(scr_ws.cell(r, cols["expected"]).value) for g, cols in hm["groups"].items()}
            text = " ".join([desc, pre, expected.get("Desktop", "")])
            hints, tiers = flags_for(text)
            vague = [w for w in VAGUE_WORDS if re.search(r"\b%s\b" % re.escape(w), expected.get("Desktop", "").lower())]
            steps.append({
                "step": step_no or "?",
                "row": r,
                "description": desc,
                "precondition": pre,
                "expected": expected,
                "hints": hints,
                "tiers_mentioned": tiers,
                "starred": step_no.endswith("*"),
                "ambiguous_expected": vague,
            })
        blocks.append({"block_start_row": r0, "explicit_id": explicit, "scenario": scenario,
                       "component": component, "columns": hm, "steps": steps})

    # Resolve IDs: explicit first, then fill broken (#REF!) ones in plan order.
    claimed = {b["explicit_id"] for b in blocks if b["explicit_id"]}
    unclaimed = [sid for sid in plan_order if sid not in claimed]
    scripts = []
    for b in blocks:
        if b["explicit_id"]:
            sid, src = b["explicit_id"], "explicit"
        elif unclaimed:
            sid, src = unclaimed.pop(0), "inferred_by_order"
        else:
            warnings.append("Block at row %d has no ID and no unclaimed plan ID left" % b["block_start_row"])
            continue
        p = plan.get(sid, {})
        if not p:
            warnings.append("%s (row %d) not found in the plan sheet" % (sid, b["block_start_row"]))
        scenario = b["scenario"] or p.get("scenario", "")
        scripts.append({
            "id": sid,
            "id_source": src,
            "journey": p.get("journey", ""),
            "sub_journey": p.get("sub_journey", ""),
            "section": p.get("section", ""),
            "test_case": p.get("test_case", ""),
            "scenario": scenario,
            "plan_scenario": p.get("scenario", ""),
            "component": b["component"],
            "applicable": p.get("applicable", {"Web": True, "Web Resp": True, "App": False}),
            "plan_row": p.get("row"),
            "plan_columns": p.get("plan_columns", {}),
            "block_start_row": b["block_start_row"],
            "columns": b["columns"],
            "steps": b["steps"],
        })
    missing = [sid for sid in plan_order if sid not in {s["id"] for s in scripts}]
    for sid in missing:
        warnings.append("%s is in the plan sheet but has no script block" % sid)
    scripts.sort(key=lambda s: script_key(s["id"]))
    return scripts, warnings


# ---------------------------------------------------------------- UAT2 layout (External UAT Cycle 2 workbook)
# Scenario tab (e.g. "5.2 UAT2 R1 Test Scenarios"): header row with AMEA/AUNZ/EUAM applicability, Variation,
# Test Case ID, Sub-Test Case ID, Journey, Sub-Journey, Functionality, Test Case, User Story, Applicable to Web/App,
# then per-region "Test Status" + "Test Evidence" columns under WEB TEST STATUS and APP TEST STATUS.
# Scripts tab (e.g. "6.1 UAT2 R1 WEB Test Scripts"): one block per sub test case, labels in column B
# ("Test Case ID:", "Sub Test Case ID:", "Test Component:", "Test Case:", "User Story:", "Pre-Test Criteria:"),
# then "Step | Step Description | Expected Result". There are no per-step result columns, so results go to the
# scenario tab (region status/evidence) plus an added "Automated step results" sheet.
UAT2_ID = re.compile(r"^UAT2-\d+(?:-\d+)?$", re.I)
REGIONS = ["AMEA", "AUNZ", "EUAM"]


def read_uat2_scenarios(ws):
    hdr_row = next((r for r in range(1, 12) if any(norm_header(ws.cell(r, c).value) == "sub-test case id"
                                                   for c in range(1, ws.max_column + 1))), None)
    if not hdr_row:
        die("Scenario sheet %r: no 'Sub-Test Case ID' header row" % ws.title)
    hdr = {c: norm_header(ws.cell(hdr_row, c).value) for c in range(1, ws.max_column + 1)}
    band = {c: norm_header(ws.cell(hdr_row - 1, c).value) for c in range(1, ws.max_column + 1)}
    app_start = min([c for c, v in band.items() if v.startswith("app test status")] or [10 ** 6])
    first = lambda name: next((c for c, v in hdr.items() if v == name), None)
    cols = {k: first(k) for k in ("variation", "test case id", "sub-test case id", "journey", "sub-journey",
                                  "functionality", "test case", "user story", "applicable to web", "applicable to app")}
    region_cols = {}
    for reg in REGIONS:
        appl = next((c for c, v in hdr.items() if v == reg.lower()), None)
        web = {"status": None, "evidence": None}
        app = {"status": None, "evidence": None}
        for c, v in hdr.items():
            tgt = app if c >= app_start else web
            if v.startswith(reg.lower()) and "test status" in v:
                tgt["status"] = c
            elif v.startswith(reg.lower()) and "test evidence" in v:
                tgt["evidence"] = c
        region_cols[reg] = {"applicable": appl, "web": web, "app": app}
    rows, order = {}, []
    for r in range(hdr_row + 1, ws.max_row + 1):
        g = lambda k: clean(ws.cell(r, cols[k]).value) if cols.get(k) else ""
        sid = (g("sub-test case id") or g("test case id")).upper()
        if not UAT2_ID.match(sid):
            continue
        regions = {}
        for reg, rc in region_cols.items():
            regions[reg] = {
                "applicable": clean(ws.cell(r, rc["applicable"]).value).lower() in ("yes", "y") if rc["applicable"] else True,
                "web_status": clean(ws.cell(r, rc["web"]["status"]).value) if rc["web"]["status"] else "",
                "web_evidence": clean(ws.cell(r, rc["web"]["evidence"]).value) if rc["web"]["evidence"] else "",
            }
        rows[sid] = {"row": r, "test_case_id": g("test case id").upper(), "variation": g("variation"),
                     "journey": g("journey"), "sub_journey": g("sub-journey"), "functionality": g("functionality"),
                     "test_case": g("test case"), "user_story": g("user story"),
                     "web": g("applicable to web").upper().startswith("Y"), "app": g("applicable to app").upper().startswith("Y"),
                     "regions": regions}
        order.append(sid)
    return rows, order, region_cols


def parse_uat2(wb_path, cfg):
    wb = openpyxl.load_workbook(wb_path, data_only=True)
    sc_name, scr_name = cfg["sheets"]["scenarios"], cfg["sheets"]["scripts"]
    for n in (sc_name, scr_name):
        if n not in wb.sheetnames:
            die("Sheet %r not in workbook (sheets: %s)" % (n, ", ".join(wb.sheetnames)))
    plan, plan_order, region_cols = read_uat2_scenarios(wb[sc_name])
    ws = wb[scr_name]
    warnings = []
    if not plan:
        warnings.append("Scenario sheet %r has no scenario rows - every script is unmatched" % sc_name)

    def lab(r):
        for c in range(1, 4):
            v = clean(ws.cell(r, c).value)
            if v.endswith(":") or v.lower() in ("step",):
                return c, v.rstrip(":").strip().lower()
        return None, ""
    starts = []
    for r in range(1, ws.max_row + 1):
        c, l = lab(r)
        if l == "test case id":
            starts.append((r, c))
    scripts, seen = [], set()
    for bi, (r0, lc) in enumerate(starts):
        r_end = (starts[bi + 1][0] - 1) if bi + 1 < len(starts) else ws.max_row
        meta, hdr_row = {}, None
        for r in range(r0, min(r0 + 12, r_end + 1)):
            l = clean(ws.cell(r, lc).value).rstrip(":").strip().lower()
            if l == "step":
                hdr_row = r
                break
            if l:
                meta[l] = (clean(ws.cell(r, lc + 1).value), clean(ws.cell(r, lc + 2).value))
        if not hdr_row:
            warnings.append("Block at row %d: no 'Step' header row - skipped" % r0)
            continue
        tc_id, polarity = meta.get("test case id", ("", ""))
        sub_id, login_state = meta.get("sub test case id", ("", ""))
        sid = (sub_id or tc_id).upper()
        if not UAT2_ID.match(sid):
            warnings.append("Block at row %d: no usable test case ID (%r / %r) - skipped" % (r0, tc_id, sub_id))
            continue
        if sid in seen:
            warnings.append("%s appears twice in the scripts sheet (row %d) - second block skipped" % (sid, r0))
            continue
        seen.add(sid)
        pre = meta.get("pre-test criteria", ("", ""))[0]
        hc = {norm_header(ws.cell(hdr_row, c).value): c for c in range(1, ws.max_column + 1)}
        c_step, c_desc, c_exp = hc.get("step"), hc.get("step description"), hc.get("expected result")
        steps = []
        for r in range(hdr_row + 1, r_end + 1):
            raw = ws.cell(r, c_step).value
            step_no = str(int(raw)) if isinstance(raw, float) else clean(raw)
            desc = clean(ws.cell(r, c_desc).value)
            exp = clean(ws.cell(r, c_exp).value) if c_exp else ""
            if not step_no and not desc:
                continue
            text = " ".join([desc, exp, pre if not steps else ""])
            hints, tiers = flags_for(text)
            vague = [w for w in VAGUE_WORDS if re.search(r"\b%s\b" % re.escape(w), exp.lower())]
            steps.append({"step": step_no or "?", "row": r, "description": desc,
                          "precondition": pre if not steps else "",
                          "expected": {"Desktop": exp, "Webview": exp},
                          "hints": hints, "tiers_mentioned": tiers, "starred": step_no.endswith("*"),
                          "ambiguous_expected": vague})
        p = plan.get(sid) or plan.get(tc_id.upper()) or {}
        if not p:
            warnings.append("%s (scripts row %d) not found in the scenario sheet" % (sid, r0))
        component = meta.get("test component", ("", ""))[0]
        scripts.append({
            "id": sid, "id_source": "explicit", "test_case_id": tc_id.upper(),
            "polarity": polarity, "login_state": login_state,
            "journey": p.get("journey", ""), "sub_journey": p.get("sub_journey", ""),
            "section": p.get("functionality", component), "test_case": meta.get("test case", ("", ""))[0] or p.get("test_case", ""),
            "scenario": meta.get("test case", ("", ""))[0] or p.get("test_case", ""), "plan_scenario": p.get("test_case", ""),
            "user_story": meta.get("user story", ("", ""))[0] or p.get("user_story", ""),
            "component": component, "precondition": pre,
            "applicable": {"Web": p.get("web", True), "Web Resp": p.get("web", True), "App": p.get("app", False)},
            "regions": p.get("regions", {}), "variation": p.get("variation", ""),
            "plan_row": p.get("row"), "plan_columns": {}, "region_columns": region_cols,
            "block_start_row": r0, "columns": {"groups": {}}, "steps": steps,
        })
    for sid in plan_order:
        if sid not in seen and not any(s["test_case_id"] == sid for s in scripts):
            warnings.append("%s is in the scenario sheet but has no script block" % sid)
    scripts.sort(key=lambda s: script_key(s["id"]))
    return scripts, warnings


def write_report(path, scripts, warnings, wb_path):
    lines = ["# Parse report", "", "- Workbook: `%s`" % wb_path, "- Parsed: %s" % now_iso(),
             "- Scripts: %d, steps: %d" % (len(scripts), sum(len(s["steps"]) for s in scripts)), ""]
    lines += ["## Warnings", ""] + (["- " + w for w in warnings] or ["- none"]) + [""]
    inferred = [s for s in scripts if s["id_source"] != "explicit"]
    lines += ["## Script IDs recovered from broken `#REF!` cells (please spot-check)", "",
              "| ID | Component in script sheet | Scenario in plan sheet |", "|---|---|---|"]
    lines += ["| %s | %s | %s |" % (s["id"], s["component"], s["plan_scenario"]) for s in inferred]
    lines += ["", "## Scripts", "", "| ID | Journey | Steps | Web | Web Resp | Ambiguous expected | Human-only hints |",
              "|---|---|---|---|---|---|---|"]
    for s in scripts:
        amb = sum(1 for st in s["steps"] if st["ambiguous_expected"])
        hum = sorted({h for st in s["steps"] for h in st["hints"]
                      if h in ("payment", "otp_or_email_verification", "social_login", "biometric", "captcha", "native_app")})
        lines.append("| %s | %s | %d | %s | %s | %d | %s |" % (
            s["id"], s["journey"], len(s["steps"]), "Y" if s["applicable"].get("Web") else "N",
            "Y" if s["applicable"].get("Web Resp") else "N", amb, ", ".join(hum)))
    if any(s.get("regions") for s in scripts):
        lines += ["", "## Region applicability and current web status (from the scenario sheet)", "",
                  "| ID | Test case | AMEA | AUNZ | EUAM |", "|---|---|---|---|---|"]
        for s in scripts:
            cell = lambda r: ("-" if not s["regions"].get(r, {}).get("applicable", True)
                              else (s["regions"].get(r, {}).get("web_status") or "(blank)"))
            lines.append("| %s | %s | %s | %s | %s |" % (s["id"], s["scenario"][:70], cell("AMEA"), cell("AUNZ"), cell("EUAM")))
    lines += ["", "## Ambiguous expected results (tester should agree a concrete pass rule)", ""]
    for s in scripts:
        for st in s["steps"]:
            if st["ambiguous_expected"]:
                lines.append("- **%s step %s** - \"%s\" (words: %s)" % (
                    s["id"], st["step"], st["expected"].get("Desktop", "")[:160], ", ".join(st["ambiguous_expected"])))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ws")
    ap.add_argument("--workbook")
    a = ap.parse_args()
    ws = find_workspace(a.ws)
    cfg = load_config(ws)
    wb_path = Path(a.workbook).resolve() if a.workbook else workbook_path(ws, cfg)
    scripts, warnings = (parse_uat2 if cfg.get("layout") == "uat2" else parse)(wb_path, cfg)
    out = ws / "tests"
    out.mkdir(exist_ok=True)
    for pat in ("Test-Script-*.json", "UAT2-*.json"):
        for old in out.glob(pat):
            old.unlink()
    save_json(out / "tests.json", {"workbook": str(wb_path), "parsed_at": now_iso(),
                                   "scripts": scripts, "warnings": warnings})
    for s in scripts:
        save_json(out / ("%s.json" % s["id"]), s)
    write_report(out / "parse_report.md", scripts, warnings, wb_path)
    print("Parsed %d scripts / %d steps from %s" % (len(scripts), sum(len(s["steps"]) for s in scripts), wb_path.name))
    print("IDs recovered from #REF! cells: %d" % sum(1 for s in scripts if s["id_source"] != "explicit"))
    print("Steps with ambiguous expected results: %d" % sum(1 for s in scripts for st in s["steps"] if st["ambiguous_expected"]))
    print("Warnings: %d  -> see %s" % (len(warnings), out / "parse_report.md"))


if __name__ == "__main__":
    main()
