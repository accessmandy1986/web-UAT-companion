#!/usr/bin/env python3
"""Run bookkeeping for automated UAT: create runs, record step results + evidence,
summarise, build the HTML run report, and write results back into a copy of the workbook.

Commands:
  init      --scripts 001-016 [--viewports desktop,mobile] [--region AMEA] [--open-only] [--human-mode ask|skip] [--label TEXT]  -> RUN_ID
            (UAT2 IDs: --scripts UAT2-024 or UAT2-001-02 or 001-063; --open-only skips scenarios already Passed
             for that region; scenarios not applicable to the region are recorded as not_applicable)
  plan      --run RUN_ID [--viewport desktop] [--script ID]   steps still to execute (JSON)
  record    --run RUN_ID  < step.json   (one object or a list)  copies evidence into the run
  status    --run RUN_ID                                          progress table
  finalize  --run RUN_ID                summary.json + report.html + results workbook copy
  export-xlsx --run RUN_ID              (re)write the results workbook copy only
  latest                                prints the most recent RUN_ID

Step record fields (JSON):
  script, step, viewport                        required
  status: pass | fail | blocked | needs_human | not_applicable
  actual: what Claude observed (plain, factual)
  confidence: high | medium | low               (how sure the pass/fail judgement is)
  url, account (key from accounts.local.json, never the password)
  evidence: [paths to screenshots/GIFs]         files are copied into runs/<RUN>/evidence/
  console_errors: [str], network_failures: [{method,url,status}]
  actions: [{type, ...}]                        replayable actions, used for Playwright export
  severity: Critical | High | Medium | Low      (for fails), defect_title: short title
  blocked_by: "<script>#<step>" , notes: str
  category: FE | CMS | Content, category_reason: str     (defect type tag for the Jira title)
  extra_defects: [{defect_title, actual, severity, category, category_reason, repro_steps, evidence, ...}]
                  further independent problems seen at the same step (each becomes its own draft;
                  evidence here must be paths already inside the run, e.g. "evidence/<file>")
"""
import argparse
import html
import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path

from uat_common import (die, expand_selection, find_workspace, load_config, load_json, now_iso,
                        save_json, script_key, script_num, workbook_path)

STATUSES = ["pass", "fail", "blocked", "needs_human", "not_applicable"]
XL_STATUS = {"pass": "Pass", "fail": "Fail", "blocked": "Blocked", "needs_human": "Not Run - Manual",
             "not_applicable": "N/A"}
SEVERITIES = ["Critical", "High", "Medium", "Low"]


def run_dir(ws, run_id):
    d = ws / "runs" / run_id
    if not (d / "run.json").exists():
        die("Run %s not found in %s" % (run_id, ws / "runs"))
    return d


def tests_index(ws):
    t = load_json(ws / "tests" / "tests.json")
    return {s["id"]: s for s in t["scripts"]}


def key(script, step, viewport):
    return "%s|%s|%s" % (script, step, viewport)


def applicable(script, viewport, cfg):
    flag = cfg["viewports"][viewport]["plan_flag"]
    return bool(script["applicable"].get(flag, True))


def expected_for(step, viewport, cfg):
    grp = cfg["viewports"][viewport]["workbook_group"]
    exp = step["expected"].get(grp, "")
    if not exp and grp != "Desktop":
        return step["expected"].get("Desktop", ""), True
    return exp, False


# ---------------------------------------------------------------- commands
def cmd_init(ws, cfg, a):
    idx = tests_index(ws)
    ids = expand_selection(a.scripts, sorted(idx, key=script_key))
    if not ids:
        die("No scripts matched %r" % a.scripts)
    region = (a.region or cfg.get("default_region") or "").upper() or None
    if region and cfg.get("regions") and region not in cfg["regions"]:
        die("Unknown region %r (configured: %s)" % (region, ", ".join(cfg["regions"])))
    skipped_done = []
    if a.open_only and region:
        done = {x.lower() for x in cfg.get("skip_statuses", ["Passed"])}
        skipped_done = [i for i in ids if (idx[i].get("regions", {}).get(region, {}).get("web_status") or "").lower() in done]
        ids = [i for i in ids if i not in skipped_done]
        if not ids:
            die("Every selected script is already %s for %s" % ("/".join(cfg.get("skip_statuses", ["Passed"])), region))
    vps = [v.strip() for v in a.viewports.split(",") if v.strip()]
    for v in vps:
        if v not in cfg["viewports"]:
            die("Unknown viewport %r (configured: %s)" % (v, ", ".join(cfg["viewports"])))
    run_id = "RUN-" + now_iso().replace("-", "").replace(":", "").replace("T", "-")[:13]
    d = ws / "runs" / run_id
    base, n = run_id, 0
    while d.exists():  # several runs started in the same minute
        n += 1
        run_id = base + "bcdefghijklmnopqrstuvwxyz"[n - 1]
        d = ws / "runs" / run_id
    (d / "evidence").mkdir(parents=True)
    results = {}
    for sid in ids:
        s = idx[sid]
        for v in vps:
            for st in s["steps"]:
                exp, fb = expected_for(st, v, cfg)
                r = {"script": sid, "step": st["step"], "viewport": v, "status": "pending",
                     "expected": exp, "expected_fallback_to_desktop": fb}
                if not applicable(s, v, cfg):
                    r.update(status="not_applicable", actual="Not applicable to this platform per test plan",
                             confidence="high", recorded_at=now_iso())
                elif region and not s.get("regions", {}).get(region, {}).get("applicable", True):
                    r.update(status="not_applicable", actual="Not applicable to %s per the scenario sheet" % region,
                             confidence="high", recorded_at=now_iso())
                elif region and "not test" in (s.get("regions", {}).get(region, {}).get("web_status") or "").lower():
                    r.update(status="not_applicable", confidence="high", recorded_at=now_iso(),
                             actual="Out of scope for %s: scenario sheet status is '%s'" % (
                                 region, s["regions"][region]["web_status"]))
                results[key(sid, st["step"], v)] = r
    run = {"run_id": run_id, "label": a.label or "", "region": region, "human_mode": a.human_mode, "skipped_already_passed": skipped_done, "created": now_iso(), "base_url": cfg["base_url"],
           "environment": cfg.get("environment_name", ""), "viewports": vps, "scripts": ids,
           "tester": cfg.get("tester_name", "Claude"), "results": results, "finalized": None}
    save_json(d / "run.json", run)
    print(run_id)
    print("Created %s: %d scripts x %d viewports, %d step results to fill (%d not applicable)." % (
        d, len(ids), len(vps), sum(1 for r in results.values() if r["status"] == "pending"),
        sum(1 for r in results.values() if r["status"] == "not_applicable")))
    if region:
        print("Region: %s%s" % (region, (" - skipped %d already-passed: %s" % (len(skipped_done), ", ".join(skipped_done))) if skipped_done else ""))
    print("Human steps: %s" % ("ASK - pause and prompt the tester, then continue the step" if a.human_mode == "ask"
                               else "SKIP - record needs_human and move on to the next test case"))


def cmd_plan(ws, cfg, a):
    d = run_dir(ws, a.run)
    run = load_json(d / "run.json")
    idx = tests_index(ws)
    td = load_json(ws / "config" / "test_data.json", default={})
    td_rule = None
    region = run.get("region")
    if td.get("properties"):
        props = [p for p in td["properties"] if not region or p.get("region", region) == region] or td["properties"]
        cities = sorted({p["city"] for p in props})
        default = (td.get("by_region", {}).get(region, {}) or {}).get("default_destination") or td.get("default_destination", cities[0])
        td_rule = ("ONLY use UAT properties from config/test_data.json%s: %s. Default destination: %s. "
                   "Other destinations return 0 results by design - not a defect." % (
                       " for region %s" % region if region else "",
                       ", ".join("%s (%s)" % (p["city"], p["code"]) for p in props), default))
    out = []
    for v in run["viewports"]:
        if a.viewport and v != a.viewport:
            continue
        for sid in run["scripts"]:
            if a.script and sid not in expand_selection(a.script, [sid]):
                continue
            s = idx[sid]
            pend = []
            for st in s["steps"]:
                r = run["results"][key(sid, st["step"], v)]
                if r["status"] == "pending":
                    pend.append({"step": st["step"], "description": st["description"],
                                 "precondition": st["precondition"], "expected": r["expected"],
                                 "expected_is_desktop_fallback": r["expected_fallback_to_desktop"],
                                 "hints": st["hints"], "ambiguous_words": st["ambiguous_expected"]})
            if pend:
                out.append({"viewport": v, "size": [cfg["viewports"][v]["width"], cfg["viewports"][v]["height"]],
                            "script": sid, "scenario": s["scenario"], "component": s["component"],
                            "journey": s["journey"], "test_data_rule": td_rule, "pending_steps": pend})
                if s.get("regions"):
                    rs = s["regions"].get(region or "", {})
                    out[-1].update({"region": region, "user_story": s.get("user_story", ""),
                                    "precondition": s.get("precondition", ""), "login_state": s.get("login_state", ""),
                                    "polarity": s.get("polarity", ""), "current_status": rs.get("web_status", ""),
                                    "existing_jira": rs.get("web_evidence", "")})
                if a.first:
                    print(json.dumps(out, indent=2, ensure_ascii=False))
                    return
    print(json.dumps(out, indent=2, ensure_ascii=False))
    if not out:
        print("# Nothing pending - run `finalize`.", file=sys.stderr)


def secret_map(ws):
    """Literal secret -> placeholder, so passwords never land in results, reports, tickets or Playwright specs."""
    acc = load_json(ws / "config" / "accounts.local.json", default={})
    m = {}
    gate = acc.get("site_gate", {}).get("password")
    if gate:
        m[gate] = "{{SITE_PASSWORD}}"
    for k, v in (acc.get("accounts") or {}).items():
        if v.get("password"):
            m.setdefault(v["password"], "{{%s.password}}" % k)
    return m


def scrub_secrets(ws, obj):
    m = secret_map(ws)

    def walk(x):
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        if isinstance(x, str):
            for lit, ph in m.items():
                x = x.replace(lit, ph)
        return x
    clean_obj = walk(obj)
    obj.clear()
    obj.update(clean_obj)
    return obj


def cmd_record(ws, cfg, a):
    d = run_dir(ws, a.run)
    run = load_json(d / "run.json")
    raw = sys.stdin.read() if not a.file else Path(a.file).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except ValueError as e:
        die("record expects JSON on stdin: %s" % e)
    recs = data if isinstance(data, list) else [data]
    for rec in recs:
        for f in ("script", "step", "viewport", "status"):
            if f not in rec:
                die("record missing %r: %s" % (f, rec))
        rec["step"] = str(rec["step"])
        if rec["status"] not in STATUSES:
            die("status must be one of %s" % STATUSES)
        if rec.get("severity") and rec["severity"] not in SEVERITIES:
            die("severity must be one of %s" % SEVERITIES)
        k = key(rec["script"], rec["step"], rec["viewport"])
        if k not in run["results"]:
            die("No such step in this run: %s" % k)
        scrub_secrets(ws, rec)
        copied = []
        for i, p in enumerate(rec.get("evidence", []) or []):
            src = Path(p).expanduser()
            if not src.exists():
                print("warning: evidence file not found, skipped: %s" % src, file=sys.stderr)
                continue
            safe = "%s_%s_s%s_%d%s" % (rec["viewport"], rec["script"], rec["step"].replace("*", "x"), i, src.suffix.lower())
            dst = d / "evidence" / safe
            shutil.copy2(src, dst)
            copied.append("evidence/" + safe)
        prev = run["results"][k]
        if a.append:
            merged = dict(prev)
        else:  # re-recording replaces the earlier verdict entirely (no stale severity/defect fields)
            merged = {kk: prev[kk] for kk in ("script", "step", "viewport", "expected", "expected_fallback_to_desktop") if kk in prev}
            if prev.get("status") not in (None, "pending"):
                merged["previous"] = {kk: prev.get(kk) for kk in ("status", "actual", "recorded_at")}
        merged.update({kk: vv for kk, vv in rec.items() if kk != "evidence"})
        merged["evidence"] = (prev.get("evidence") or []) + copied if a.append else copied
        merged["recorded_at"] = now_iso()
        run["results"][k] = merged
    save_json(d / "run.json", run)
    print("Recorded %d step result(s)." % len(recs))


def summarise(run):
    by = {}
    for r in run["results"].values():
        b = by.setdefault((r["script"], r["viewport"]), {s: 0 for s in STATUSES + ["pending"]})
        b[r["status"]] += 1
    rows = []
    for (sid, v), c in sorted(by.items(), key=lambda x: (x[0][1], script_key(x[0][0]))):
        if c["fail"]:
            overall = "fail"
        elif c["blocked"]:
            overall = "blocked"
        elif c["pending"]:
            overall = "pending"
        elif c["needs_human"]:
            overall = "needs_human"
        elif c["pass"]:
            overall = "pass"
        else:
            overall = "not_applicable"
        rows.append({"script": sid, "viewport": v, "overall": overall, **c})
    tot = {s: sum(1 for r in run["results"].values() if r["status"] == s) for s in STATUSES + ["pending"]}
    return rows, tot


def cmd_status(ws, cfg, a):
    run = load_json(run_dir(ws, a.run) / "run.json")
    rows, tot = summarise(run)
    print("%-16s %-8s %-13s %5s %5s %5s %5s %5s %5s" % ("script", "viewport", "overall", "pass", "fail", "block", "human", "n/a", "todo"))
    for r in rows:
        print("%-16s %-8s %-13s %5d %5d %5d %5d %5d %5d" % (r["script"], r["viewport"], r["overall"], r["pass"], r["fail"],
                                                          r["blocked"], r["needs_human"], r["not_applicable"], r["pending"]))
    print("TOTAL", json.dumps(tot))


def defect_refs(ws, run_id):
    """result key -> defect reference (Jira key if filed, else draft id)."""
    reg = load_json(ws / "defects" / "registry.json", default={"defects": {}})
    refs = {}
    for did, dfx in reg["defects"].items():
        if dfx.get("status") == "rejected":
            continue
        ref = dfx.get("jira_key") or did
        for occ in dfx.get("occurrences", []):
            if occ.get("run") == run_id:
                refs[occ["result_key"]] = ref
    return refs


def export_uat2(ws, cfg, run, d, quiet=False):
    """UAT2 workbook: no per-step result columns, so (1) set the run region's WEB Test Status / Test Evidence on the
    scenario sheet and (2) add an 'Automated step results' sheet with one row per step and viewport."""
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    idx = tests_index(ws)
    src = workbook_path(ws, cfg)
    dst = d / ("%s_RESULTS_%s.xlsx" % (src.stem, run["run_id"]))
    wb = openpyxl.load_workbook(src)
    sc = wb[cfg["sheets"]["scenarios"]]
    refs = defect_refs(ws, run["run_id"])
    words = cfg.get("status_words", {})
    region = run.get("region") or cfg.get("default_region")
    rows, _ = summarise(run)
    per_script = {}
    for row in rows:
        per_script.setdefault(row["script"], []).append(row["overall"])
    updated = []
    for sid, overalls in per_script.items():
        s = idx.get(sid)
        if not s or not s.get("plan_row") or not region:
            continue
        rc = (s.get("region_columns") or {}).get(region, {}).get("web", {})
        if not rc.get("status"):
            continue
        if set(overalls) <= {"not_applicable", "pending"}:  # nothing executed - leave the human's status alone
            continue
        mine = sorted({refs[k] for k, r in run["results"].items() if r["script"] == sid and k in refs})
        if "fail" in overalls:
            status = words.get("fail_with_defect", "Defect Raised") if mine else words.get("fail", "Failed")
        elif "blocked" in overalls:
            status = words.get("blocked", "Unable to test")
        elif "pending" in overalls or "needs_human" in overalls:
            status = words.get("in_progress", "Test In Progress")
        else:
            status = words.get("pass", "Passed")
        sc.cell(s["plan_row"], rc["status"]).value = status
        if rc.get("evidence"):
            prev = str(sc.cell(s["plan_row"], rc["evidence"]).value or "").strip()
            new = [m for m in mine if m not in prev]
            if new:
                sc.cell(s["plan_row"], rc["evidence"]).value = "\n".join(([prev] if prev else []) + new)
        updated.append("%s=%s" % (sid, status))
    name = "Automated step results"
    if name in wb.sheetnames:
        del wb[name]
    sh = wb.create_sheet(name)
    hdr = ["Run", "Region", "Sub-Test Case ID", "Test Case", "Step", "Step Description", "Expected Result", "Viewport",
           "Result", "Actual Result", "Defect", "Confidence", "Evidence", "Tester", "Date", "Remarks"]
    sh.append(hdr)
    for c in sh[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1D2433")
    today = date.today().strftime("%d %b %Y")
    order = sorted(run["results"].items(), key=lambda kv: (script_key(kv[1]["script"]),
                   int(re.sub(r"\D", "", kv[1]["step"]) or 0), kv[1]["viewport"]))
    written = 0
    for k, r in order:
        if r["status"] == "pending":
            continue
        s = idx.get(r["script"], {})
        st = next((x for x in s.get("steps", []) if x["step"] == r["step"]), {})
        sh.append([run["run_id"], region or "", r["script"], s.get("scenario", ""), r["step"], st.get("description", ""),
                   r.get("expected", ""), r["viewport"], XL_STATUS[r["status"]], r.get("actual", ""), refs.get(k, ""),
                   r.get("confidence", ""), ", ".join(Path(e).name for e in r.get("evidence", [])), run["tester"], today,
                   r.get("notes", "")])
        written += 1
    widths = [16, 8, 14, 40, 6, 50, 50, 9, 14, 60, 14, 10, 40, 22, 12, 40]
    for i, w in enumerate(widths, start=1):
        sh.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
    for row in sh.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    sh.freeze_panes = "A2"
    wb.save(dst)
    if not quiet:
        print("Region %s: updated %d scenario status(es); wrote %d step rows into '%s' in %s" % (
            region, len(updated), written, name, dst))
    return dst


def cmd_export_xlsx(ws, cfg, a, quiet=False):
    import openpyxl
    d = run_dir(ws, a.run)
    if cfg.get("layout") == "uat2":
        return export_uat2(ws, cfg, load_json(d / "run.json"), d, quiet)
    run = load_json(d / "run.json")
    idx = tests_index(ws)
    src = workbook_path(ws, cfg)
    dst = d / ("%s_RESULTS_%s.xlsx" % (src.stem, run["run_id"]))
    wb = openpyxl.load_workbook(src)
    sws = wb[cfg["sheets"]["scripts"]]
    pws = wb[cfg["sheets"]["plan"]]
    refs = defect_refs(ws, run["run_id"])
    today = date.today().strftime("%d %b %Y")
    written = 0
    for k, r in run["results"].items():
        if r["status"] == "pending":
            continue
        s = idx.get(r["script"])
        st = next((x for x in s["steps"] if x["step"] == r["step"]), None) if s else None
        if not st:
            continue
        grp = cfg["viewports"][r["viewport"]]["workbook_group"]
        cols = s["columns"]["groups"].get(grp)
        if not cols:
            continue
        row = st["row"]
        actual = r.get("actual", "")
        if r.get("evidence"):
            actual += "\nEvidence: " + ", ".join(Path(e).name for e in r["evidence"])
        sws.cell(row, cols["actual"]).value = actual.strip()
        sws.cell(row, cols["pass_fail"]).value = XL_STATUS[r["status"]]
        if k in refs:
            sws.cell(row, cols["defect_id"]).value = refs[k]
        sws.cell(row, cols["tester"]).value = run["tester"]
        sws.cell(row, cols["test_date"]).value = today
        remarks = []
        if r.get("confidence") and r["confidence"] != "high":
            remarks.append("Confidence: %s - please verify" % r["confidence"])
        if r.get("expected_fallback_to_desktop"):
            remarks.append("No Webview expected result in script; judged against Desktop expected result")
        if r.get("notes"):
            remarks.append(r["notes"])
        remarks.append("Automated run %s" % run["run_id"])
        sws.cell(row, cols["remarks"]).value = "; ".join(remarks)
        written += 1
    rows, _ = summarise(run)
    per_script = {}
    for row in rows:
        per_script.setdefault(row["script"], []).append(row["overall"])
    for sid, overalls in per_script.items():
        s = idx[sid]
        pc = s.get("plan_columns") or {}
        if not s.get("plan_row"):
            continue
        status = ("Fail" if "fail" in overalls else "Blocked" if "blocked" in overalls else
                  "In Progress" if "pending" in overalls or "needs_human" in overalls else
                  "Pass" if "pass" in overalls else "N/A")
        if pc.get("status"):
            pws.cell(s["plan_row"], pc["status"]).value = status
        if pc.get("actual tester"):
            pws.cell(s["plan_row"], pc["actual tester"]).value = run["tester"]
        if pc.get("actual start date"):
            pws.cell(s["plan_row"], pc["actual start date"]).value = run["created"][:10]
        if pc.get("actual end date") and status in ("Pass", "Fail", "N/A"):
            pws.cell(s["plan_row"], pc["actual end date"]).value = (run.get("finalized") or now_iso())[:10]
    wb.save(dst)
    if not quiet:
        print("Wrote %d step results into %s" % (written, dst))
    return dst


def esc(x):
    return html.escape(str(x or ""))


def build_report(ws, cfg, run, d):
    idx = tests_index(ws)
    rows, tot = summarise(run)
    refs = defect_refs(ws, run["run_id"])
    executed = sum(v for k, v in tot.items() if k not in ("pending", "not_applicable"))
    pass_rate = (100.0 * tot["pass"] / (tot["pass"] + tot["fail"])) if (tot["pass"] + tot["fail"]) else 0
    chip = {"pass": "#1f7a4d", "fail": "#b42318", "blocked": "#b54708", "needs_human": "#5b4bc4",
            "not_applicable": "#667085", "pending": "#98a2b3"}
    css = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d2433;--mute:#5d6676;--line:#e3e3df;--brand:#7a1f2b}
@media (prefers-color-scheme:dark){:root{--bg:#15171c;--card:#1d2027;--ink:#e8e9ec;--mute:#a2a8b4;--line:#2d313a;--brand:#e07a86}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1200px;margin:0 auto;padding:24px 16px}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 10px}
.mute{color:var(--mute)}.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:16px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}.kpi b{display:block;font-size:22px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{font-size:12px;color:var(--mute);font-weight:600}
.chip{display:inline-block;padding:1px 8px;border-radius:99px;color:#fff;font-size:12px;white-space:nowrap}
details{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:8px 0;padding:8px 12px}
summary{cursor:pointer;font-weight:600}img.ev{max-width:320px;border:1px solid var(--line);border-radius:6px;margin:4px 4px 0 0}
.wrap{overflow-x:auto}code{font-size:12px}
"""
    h = ["<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
         "<title>UAT Run %s</title><style>%s</style></head><body><main>" % (esc(run["run_id"]), css),
         "<h1>UAT run %s</h1><div class='mute'>%s &middot; %s &middot; viewports: %s &middot; tester: %s</div>" % (
             esc(run["run_id"]), esc(run["environment"] + (" · region " + run["region"] if run.get("region") else "") + (" · human steps: " + run["human_mode"] if run.get("human_mode") else "")), esc(run["base_url"]), esc(", ".join(run["viewports"])), esc(run["tester"])),
         "<div class='kpis'>"]
    for label, val in [("Steps executed", executed), ("Passed", tot["pass"]), ("Failed", tot["fail"]),
                       ("Blocked", tot["blocked"]), ("Needs human", tot["needs_human"]), ("Pass rate", "%.0f%%" % pass_rate)]:
        h.append("<div class='kpi'><span class='mute'>%s</span><b>%s</b></div>" % (label, val))
    h.append("</div><h2>By script</h2><div class='wrap'><table><tr><th>Script</th><th>Scenario</th><th>Viewport</th><th>Result</th><th>Pass</th><th>Fail</th><th>Blocked</th><th>Human</th></tr>")
    for r in rows:
        s = idx.get(r["script"], {})
        h.append("<tr><td>%s</td><td>%s</td><td>%s</td><td><span class='chip' style='background:%s'>%s</span></td><td>%d</td><td>%d</td><td>%d</td><td>%d</td></tr>" % (
            esc(r["script"]), esc(s.get("scenario", ""))[:120], esc(r["viewport"]), chip[r["overall"]], esc(r["overall"]),
            r["pass"], r["fail"], r["blocked"], r["needs_human"]))
    h.append("</table></div><h2>Failures, blocks and manual items</h2>")
    issues = [r for r in run["results"].values() if r["status"] in ("fail", "blocked", "needs_human")]
    issues.sort(key=lambda r: (["fail", "blocked", "needs_human"].index(r["status"]), script_key(r["script"])))
    if not issues:
        h.append("<p class='mute'>None.</p>")
    for r in issues:
        k = key(r["script"], r["step"], r["viewport"])
        st = next((x for x in idx[r["script"]]["steps"] if x["step"] == r["step"]), {})
        ev = "".join("<a href='%s'><img class='ev' src='%s' alt='evidence'></a>" % (esc(e), esc(e))
                     if e.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")) else "<a href='%s'>%s</a> " % (esc(e), esc(e))
                     for e in r.get("evidence", []))
        h.append("<details %s><summary><span class='chip' style='background:%s'>%s</span> %s step %s &middot; %s %s</summary>" % (
            "open" if r["status"] == "fail" else "", chip[r["status"]], esc(r["status"]), esc(r["script"]), esc(r["step"]),
            esc(r["viewport"]), ("&middot; " + esc(refs[k])) if k in refs else ""))
        h.append("<p><b>Step:</b> %s</p><p><b>Expected:</b> %s</p><p><b>Actual:</b> %s</p>" % (
            esc(st.get("description")), esc(r.get("expected")), esc(r.get("actual"))))
        if r.get("severity"):
            h.append("<p><b>Proposed severity:</b> %s &middot; <b>confidence:</b> %s</p>" % (esc(r["severity"]), esc(r.get("confidence"))))
        if r.get("url"):
            h.append("<p><b>URL:</b> <code>%s</code></p>" % esc(r["url"]))
        if r.get("console_errors"):
            h.append("<p><b>Console errors:</b></p><pre>%s</pre>" % esc("\n".join(r["console_errors"])[:3000]))
        if r.get("network_failures"):
            h.append("<p><b>Failed requests:</b></p><pre>%s</pre>" % esc("\n".join(
                "%s %s -> %s" % (n.get("method", ""), n.get("url", ""), n.get("status", "")) for n in r["network_failures"])[:3000]))
        if r.get("notes"):
            h.append("<p><b>Notes:</b> %s</p>" % esc(r["notes"]))
        h.append(ev + "</details>")
    h.append("<p class='mute'>Generated %s by web-uat-companion. Evidence files are in the <code>evidence/</code> folder next to this report.</p></main></body></html>" % now_iso())
    (d / "report.html").write_text("\n".join(h), encoding="utf-8")
    save_json(d / "summary.json", {"run_id": run["run_id"], "totals": tot, "by_script": rows, "pass_rate": pass_rate})


def cmd_finalize(ws, cfg, a):
    d = run_dir(ws, a.run)
    run = load_json(d / "run.json")
    rows, tot = summarise(run)
    run["finalized"] = now_iso()
    save_json(d / "run.json", run)
    build_report(ws, cfg, run, d)
    x = cmd_export_xlsx(ws, cfg, a, quiet=True)
    print("Report:   %s" % (d / "report.html"))
    print("Workbook: %s" % x)
    print("Totals:   %s" % json.dumps(tot))
    if tot["pending"]:
        print("NOTE: %d steps still pending (not executed)." % tot["pending"])


def cmd_latest(ws, cfg, a):
    runs = sorted((p.name for p in (ws / "runs").glob("RUN-*") if (p / "run.json").exists()))
    if not runs:
        die("No runs yet")
    print(runs[-1])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ws")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("--scripts", required=True)
    p.add_argument("--viewports", default="desktop,mobile"); p.add_argument("--label")
    p.add_argument("--region", help="AMEA | AUNZ | EUAM (UAT2 workbook); default from config")
    p.add_argument("--open-only", action="store_true", help="skip scenarios already Passed for the region")
    p.add_argument("--human-mode", choices=["ask", "skip"], required=True,
                   help="ask = pause and prompt the tester at each human step; skip = mark needs_human and continue")
    p = sub.add_parser("plan"); p.add_argument("--run", required=True); p.add_argument("--viewport")
    p.add_argument("--script"); p.add_argument("--first", action="store_true", help="only the next script with pending steps")
    p = sub.add_parser("record"); p.add_argument("--run", required=True); p.add_argument("--file")
    p.add_argument("--append", action="store_true", help="keep earlier evidence when re-recording a step")
    for c in ("status", "finalize", "export-xlsx"):
        p = sub.add_parser(c); p.add_argument("--run", required=True)
    sub.add_parser("latest")
    a = ap.parse_args()
    ws = find_workspace(a.ws)
    cfg = load_config(ws)
    {"init": cmd_init, "plan": cmd_plan, "record": cmd_record, "status": cmd_status, "finalize": cmd_finalize,
     "export-xlsx": cmd_export_xlsx, "latest": cmd_latest}[a.cmd](ws, cfg, a)


if __name__ == "__main__":
    main()
