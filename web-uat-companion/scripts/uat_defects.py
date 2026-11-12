#!/usr/bin/env python3
"""Turn failed UAT steps into Jira-ready defect drafts (UAT defect template), track approval and filing.

Commands:
  draft   --run RUN_ID            create/refresh drafts from the run's failed steps (de-duplicated)
  list    [--status draft|approved|filed|rejected] [--run RUN_ID]
  show    DEF-0001                print the Jira-ready markdown
  approve DEF-0001 [DEF-0002 ...]
  reject  DEF-0001 --reason "not a bug: ..."
  edit    DEF-0001 --title "..." | --severity High | --steps-file steps.txt | --category CMS   (human corrections)
  jira-payload DEF-0001           JSON with all fields for the Atlassian connector create call
  mark-filed DEF-0001 PROJ-1234 record the Jira key (also flows into the results workbook)
  csv     --run RUN_ID            Jira CSV import file for approved defects (fallback if no connector)

Drafts live in <ws>/defects/registry.json and <ws>/defects/<RUN_ID>/ (markdown + review.html).
"""
import argparse
import csv
import hashlib
import html
import json
import re
import sys
from pathlib import Path

from uat_common import die, find_workspace, load_config, load_json, now_iso, save_json, script_key, script_label, script_num

PRIORITY = {"Critical": "Highest", "High": "High", "Medium": "Medium", "Low": "Low"}
VP_LABEL = {"desktop": "Desktop", "mobile": "Mobile (responsive)"}
# Defect type tag shown in the Jira title, e.g. "[Web] [FE] - ...". Definitions agreed with the UAT lead (Nov 2026).
CATEGORIES = {
    "FE": "Front-end website - likely requires a code change",
    "CMS": "Content management system (Sitecore) - likely requires a code change",
    "Content": "Editable text or image - no code change needed",
}


def norm_cat(c):
    c = (c or "").strip().strip("[]")
    return next((k for k in CATEGORIES if k.lower() == c.lower()), None)


def reg_path(ws):
    return ws / "defects" / "registry.json"


def load_reg(ws):
    return load_json(reg_path(ws), default={"next": 1, "defects": {}})


def norm(t):
    return re.sub(r"[^a-z0-9 ]", "", (t or "").lower())[:80].strip()


def fingerprint(script, step, title):
    return hashlib.sha1(("%s|%s|%s" % (script, step, norm(title))).encode()).hexdigest()[:12]


def prefix_for(viewports, cfg):
    p = [cfg["viewports"][v]["jira_prefix"].strip("[]") for v in sorted(set(viewports)) if v in cfg["viewports"]]
    return "[%s]" % "/".join(p) if p else "[Web]"


def derive_title(st_desc, actual):
    a = (actual or "").strip().split("\n")[0]
    a = re.sub(r"\s+", " ", a)
    return (a[:87] + "...") if len(a) > 90 else (a or "Step fails: " + st_desc[:70])


def build_markdown(dfx, cfg):
    j = cfg["jira"]
    steps = dfx["repro_steps"]
    cat = dfx.get("category") or "FE"
    lines = ["## %s" % j.get("description_heading", "UAT Functional Defect"), "",
             "**Defect type:** [%s] %s%s" % (cat, CATEGORIES.get(cat, ""),
                                             (" - %s" % dfx["category_reason"]) if dfx.get("category_reason") else ""), "",
             "**Steps to Replicate the Issue:**", ""]
    lines += ["%d. %s" % (i, s) for i, s in enumerate(steps, 1)]
    lines += ["", "---", "", "**Actual Results:**", "", dfx["actual"], "",
              "**Expected Results:**", "", dfx["expected"], "",
              "**Attach the screenshot:**", ""]
    lines += ["- %s" % n for n in dict.fromkeys(Path(e).name for e in dfx["evidence"])] or ["- (none captured)"]
    lines += ["", "---", "", "**Technical details (automated UAT)**", "",
              "- Test script: %s step %s - %s" % (dfx["script"], dfx["step"], dfx["scenario"]),
              "- Occurrences: %s" % (", ".join(sorted({"%s step %s (%s)" % tuple(o["result_key"].split("|"))
                                                         for o in dfx["occurrences"]})) or "-"),
              "- Environment: %s" % cfg["base_url"],
              "- Affected: %s (Chrome)" % ", ".join("%s %dx%d" % (VP_LABEL.get(v, v), cfg["viewports"][v]["width"], cfg["viewports"][v]["height"])
                                                   for v in dfx["viewports"] if v in cfg["viewports"]),
              "- Page URL: %s" % (dfx.get("url") or "n/a")]
    if dfx.get("account"):
        lines.append("- Test account tier: %s" % dfx["account"])
    if dfx.get("console_errors"):
        lines += ["- Console errors:", "```", "\n".join(dfx["console_errors"])[:2500], "```"]
    if dfx.get("network_failures"):
        lines += ["- Failed network requests:", "```", "\n".join(
            "%s %s -> %s" % (n.get("method", ""), n.get("url", ""), n.get("status", "")) for n in dfx["network_failures"])[:2500], "```"]
    lines.append("- Severity: %s · Detection confidence: %s · Run(s): %s" % (
        dfx["severity"], dfx.get("confidence", "n/a"), ", ".join(sorted({o["run"] for o in dfx["occurrences"]}))))
    return "\n".join(lines)


def full_title(dfx, cfg):
    return "%s [%s] - %s" % (prefix_for(dfx["viewports"], cfg), dfx.get("category") or "FE", dfx["title"])


def account_label(ws, key):
    acc = load_json(ws / "config" / "accounts.local.json", default={}).get("accounts", {})
    a = acc.get(key or "")
    return ("%s member (%s)" % (a.get("tier", ""), key)) if a else (key or "")


def cmd_draft(ws, cfg, a):
    rd = ws / "runs" / a.run
    run = load_json(rd / "run.json")
    region = run.get("region")
    tests = {s["id"]: s for s in load_json(ws / "tests" / "tests.json")["scripts"]}
    reg = load_reg(ws)
    fails = []
    for r in run["results"].values():
        if r["status"] == "fail":
            fails.append(r)
        # A step can expose more than one independent problem: "extra_defects": [{defect_title, actual, severity,
        # category, category_reason, repro_steps, evidence, confidence, url}] - each becomes its own draft.
        for x in r.get("extra_defects") or []:
            fails.append(dict(r, **{k: v for k, v in x.items()}, status="fail", extra_defects=None))
    # One defect per root cause. A runner-supplied defect_title is the dedupe key, so the same bug seen
    # on several viewports, steps or scripts becomes ONE ticket listing every occurrence. Failures without
    # a defect_title fall back to script+step+derived title.
    groups = {}
    for r in fails:
        title = r.get("defect_title") or derive_title("", r.get("actual"))
        gk = ("title", "", norm(title)) if r.get("defect_title") else (r["script"], r["step"], norm(title))
        groups.setdefault(gk, []).append((r, title))
    for gk in groups:  # most-severe / first occurrence first, so it supplies repro steps and severity
        groups[gk].sort(key=lambda it: (script_key(it[0]["script"]), int(re.sub(r"\D", "", it[0]["step"]) or 0), it[0]["viewport"]))
    created, updated = [], []
    for gk, items in sorted(groups.items(), key=lambda x: (script_key(x[1][0][0]["script"]), x[1][0][0]["step"])):
        r0, title = items[0]
        sid, step = r0["script"], r0["step"]
        s = tests[sid]
        fp = fingerprint("title", "", title) if gk[0] == "title" else fingerprint(sid, step, title)
        existing = next((d for d in reg["defects"].values() if d["status"] != "rejected"
                         and (d["fingerprint"] == fp or (gk[0] == "title" and norm(d["title"]) == norm(title)))), None)
        occ = [{"run": a.run, "result_key": "%s|%s|%s" % (r["script"], r["step"], r["viewport"]), "viewport": r["viewport"],
                "seen": r.get("recorded_at")} for r, _ in items]
        evidence = list(dict.fromkeys("runs/%s/%s" % (a.run, e) for r, _ in items for e in r.get("evidence", [])))
        if existing:
            known = {o["result_key"] + o["run"] for o in existing["occurrences"]}
            existing["occurrences"] += [o for o in occ if o["result_key"] + o["run"] not in known]
            existing["viewports"] = sorted(set(existing["viewports"]) | {r["viewport"] for r, _ in items})
            existing["evidence"] = list(dict.fromkeys(existing["evidence"] + evidence))
            existing["last_seen"] = now_iso()
            if existing["status"] == "filed":
                existing["recurring"] = True
            updated.append(existing["id"])
            continue
        did = "DEF-%04d" % reg["next"]
        reg["next"] += 1
        repro = r0.get("repro_steps") or (
            (["Precondition: %s" % s["steps"][0]["precondition"]] if s["steps"] and s["steps"][0].get("precondition") else []) +
            ["Go to %s" % cfg["base_url"]] +
            [x["description"] for x in s["steps"][: [y["step"] for y in s["steps"]].index(step) + 1]])
        order = ["Critical", "High", "Medium", "Low"]
        cat = next((norm_cat(r.get("category")) for r, _ in items if norm_cat(r.get("category"))), None)
        reason = next((r.get("category_reason") for r, _ in items if r.get("category_reason")), "")
        sev = min((r.get("severity") for r, _ in items if r.get("severity") in order), key=order.index, default="Medium")
        reg["defects"][did] = {
            "id": did, "fingerprint": fp, "status": "draft", "created": now_iso(), "last_seen": now_iso(),
            "script": sid, "step": step, "scenario": s["scenario"], "journey": s["journey"], "component": s["component"],
            "title": title, "category": cat or "FE", "category_reason": reason if cat else
            "Not classified by the runner - defaulted to FE, please check", "severity": sev, "priority": PRIORITY[sev],
            "confidence": min((r.get("confidence", "high") for r, _ in items), key=["low", "medium", "high"].index),
            "viewports": sorted({r["viewport"] for r, _ in items}),
            "repro_steps": repro, "actual": r0.get("actual", ""), "expected": r0.get("expected", ""),
            "url": r0.get("url", ""), "account": account_label(ws, r0.get("account")),
            "console_errors": sorted({e for r, _ in items for e in r.get("console_errors", [])}),
            "network_failures": [n for r, _ in items for n in r.get("network_failures", [])][:20],
            "evidence": evidence, "occurrences": occ, "jira_key": None, "region": region,
        }
        created.append(did)
    save_json(reg_path(ws), reg)
    write_run_outputs(ws, cfg, a.run, reg)
    print("Drafted %d new defect(s): %s" % (len(created), ", ".join(created) or "-"))
    if updated:
        print("Matched %d existing defect(s) (duplicates / still reproducing): %s" % (len(updated), ", ".join(updated)))
    print("Review page: %s" % (ws / "defects" / a.run / "review.html"))


def write_run_outputs(ws, cfg, run_id, reg):
    out = ws / "defects" / run_id
    out.mkdir(parents=True, exist_ok=True)
    mine = [d for d in reg["defects"].values() if any(o["run"] == run_id for o in d["occurrences"])]
    # Report the viewport sizes the run really used (runs/<id>/run.json "actual_sizes": {"desktop": [w, h]}).
    actual = load_json(ws / "runs" / run_id / "run.json", default={}).get("actual_sizes") or {}
    if actual:
        cfg = json.loads(json.dumps(cfg))
        for vp, wh in actual.items():
            if vp in cfg["viewports"]:
                cfg["viewports"][vp]["width"], cfg["viewports"][vp]["height"] = int(wh[0]), int(wh[1])
    for d in mine:
        (out / ("%s.md" % d["id"])).write_text("# %s\n\n%s\n" % (full_title(d, cfg), build_markdown(d, cfg)), encoding="utf-8")
    write_review(ws, cfg, run_id, mine, out)


def write_review(ws, cfg, run_id, defects, out):
    e = html.escape
    sevc = {"Critical": "#912018", "High": "#b42318", "Medium": "#b54708", "Low": "#475467"}
    cards = []
    for d in sorted(defects, key=lambda d: (["Critical", "High", "Medium", "Low"].index(d["severity"]), d["id"])):
        imgs = "".join("<a href='../../%s' target='_blank'><img src='../../%s' alt='screenshot'></a>" % (e(p), e(p))
                       for p in d["evidence"] if p.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")))
        badge = {"draft": "Awaiting review", "approved": "Approved - ready to file", "filed": "Filed: %s" % (d.get("jira_key") or ""),
                 "rejected": "Rejected"}[d["status"]]
        warn = ""
        if d.get("confidence") in ("low", "medium"):
            warn = "<p class='warn'>Detection confidence is <b>%s</b> - please reproduce manually before approving.</p>" % e(d["confidence"])
        if d.get("recurring"):
            warn += "<p class='warn'>Already filed as %s and seen again in this run - consider a comment instead of a new ticket.</p>" % e(d.get("jira_key") or "")
        cards.append("""
<article data-id="%(id)s"><header><label><input type="checkbox" class="pick" value="%(id)s" %(chk)s %(dis)s> <b>%(id)s</b></label>
<span class="sev" style="background:%(sc)s">%(sev)s</span><span class="st">%(badge)s</span><span class="st">Type: <b>[%(cat)s]</b></span></header>
<h3>%(title)s</h3>%(warn)s
<p class="mute">%(script)s step %(step)s &middot; %(journey)s / %(component)s</p>
<details open><summary>Ticket description</summary><pre>%(md)s</pre></details>
<div class="shots">%(imgs)s</div></article>""" % dict(
            id=e(d["id"]), chk="checked" if d["status"] == "approved" else "", dis="disabled" if d["status"] in ("filed", "rejected") else "",
            sc=sevc[d["severity"]], sev=e(d["severity"]), badge=e(badge), cat=e(d.get("category") or "FE"), title=e(full_title(d, cfg)), warn=warn,
            script=e(d["script"]), step=e(d["step"]), journey=e(d["journey"]), component=e(d["component"]),
            md=e(build_markdown(d, cfg)), imgs=imgs))
    page = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Defect Review %(run)s</title><style>
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d2433;--mute:#5d6676;--line:#e3e3df;--accent:#7a1f2b;--warnbg:#fff4e5}
@media (prefers-color-scheme:dark){:root{--bg:#15171c;--card:#1d2027;--ink:#e8e9ec;--mute:#a2a8b4;--line:#2d313a;--accent:#e07a86;--warnbg:#3a2a12}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 120px}h1{font-size:22px;margin:0}h3{margin:8px 0 4px;font-size:16px}
.mute{color:var(--mute)}article{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:14px 0}
article header{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.sev{color:#fff;border-radius:99px;padding:1px 9px;font-size:12px}
.st{font-size:12px;color:var(--mute)}pre{white-space:pre-wrap;word-break:break-word;background:var(--bg);padding:10px;border-radius:8px;font-size:12.5px}
.shots img{max-width:280px;max-height:200px;border:1px solid var(--line);border-radius:6px;margin:6px 6px 0 0}
.warn{background:var(--warnbg);padding:6px 10px;border-radius:8px}
.bar{position:fixed;left:0;right:0;bottom:0;background:var(--card);border-top:1px solid var(--line);padding:12px 16px}
.bar div{max-width:980px;margin:0 auto;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
code{background:var(--bg);padding:4px 8px;border-radius:6px;font-size:12.5px;word-break:break-all;flex:1;min-width:200px}
button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:8px 14px;font-weight:600;cursor:pointer}
</style></head><body><main>
<h1>Defect review &middot; %(run)s</h1>
<p class="mute">Tick the defects that are genuine, then copy the command and paste it to Claude (or run it). Only approved defects are filed in Jira (%(proj)s, parent %(parent)s).</p>
%(cards)s
</main><div class="bar"><div><code id="cmd"></code><button id="copy">Copy approval command</button></div></div>
<script>
const box=[...document.querySelectorAll('.pick')],cmd=document.getElementById('cmd');
function upd(){const ids=box.filter(b=>b.checked&&!b.disabled).map(b=>b.value);cmd.textContent=ids.length?('Approve defects '+ids.join(' ')+' and file them in Jira'):'Tick at least one defect';}
box.forEach(b=>b.addEventListener('change',upd));upd();
document.getElementById('copy').onclick=async()=>{try{await navigator.clipboard.writeText(cmd.textContent);document.getElementById('copy').textContent='Copied';}catch(e){}};
</script></body></html>""" % dict(run=e(run_id), proj=e(cfg["jira"]["project_key"]), parent=e(cfg["jira"].get("parent_epic") or "-"),
                                    cards="\n".join(cards) or "<p>No failed steps in this run.</p>")
    (out / "review.html").write_text(page, encoding="utf-8")


def get(reg, did):
    d = reg["defects"].get(did.upper())
    if not d:
        die("Unknown defect %s" % did)
    return d


def refresh_outputs(ws, cfg, reg, dfx_list):
    for run_id in sorted({o["run"] for d in dfx_list for o in d["occurrences"]}):
        write_run_outputs(ws, cfg, run_id, reg)


def cmd_set_status(ws, cfg, a, status):
    reg = load_reg(ws)
    ds = [get(reg, i) for i in a.ids]
    for d in ds:
        if d["status"] == "filed":
            print("%s already filed as %s - unchanged" % (d["id"], d["jira_key"]))
            continue
        d["status"] = status
        d["%s_at" % status] = now_iso()
        if status == "rejected":
            d["reject_reason"] = a.reason
    save_json(reg_path(ws), reg)
    refresh_outputs(ws, cfg, reg, ds)
    print("%s: %s" % (status, ", ".join(d["id"] for d in ds)))


def cmd_edit(ws, cfg, a):
    reg = load_reg(ws)
    d = get(reg, a.id)
    if a.title:
        d["title"] = a.title
    if a.severity:
        if a.severity not in PRIORITY:
            die("severity must be one of %s" % list(PRIORITY))
        d["severity"], d["priority"] = a.severity, PRIORITY[a.severity]
    if a.steps_file:
        d["repro_steps"] = [re.sub(r"^\s*\d+[.)]\s*", "", l).strip() for l in Path(a.steps_file).read_text().splitlines() if l.strip()]
    if a.actual:
        d["actual"] = a.actual
    if a.category:
        c = norm_cat(a.category)
        if not c:
            die("category must be one of %s" % list(CATEGORIES))
        d["category"], d["category_reason"] = c, a.category_reason or "Set by reviewer"
    save_json(reg_path(ws), reg)
    refresh_outputs(ws, cfg, reg, [d])
    print("Updated %s" % d["id"])


def cmd_list(ws, cfg, a):
    reg = load_reg(ws)
    for d in sorted(reg["defects"].values(), key=lambda d: d["id"]):
        if a.status and d["status"] != a.status:
            continue
        if a.run and not any(o["run"] == a.run for o in d["occurrences"]):
            continue
        print("%-9s %-9s %-8s %-6s %-15s %s" % (d["id"], d["status"], d["severity"], d.get("confidence", ""),
                                               d.get("jira_key") or "-", full_title(d, cfg)))


def cmd_show(ws, cfg, a):
    d = get(load_reg(ws), a.id)
    print("# " + full_title(d, cfg) + "\n")
    print(build_markdown(d, cfg))


def cmd_payload(ws, cfg, a):
    d = get(load_reg(ws), a.id)
    if d["status"] != "approved" and not a.force:
        die("%s is '%s' - only approved defects can be filed (use approve first)" % (d["id"], d["status"]))
    j = cfg["jira"]
    payload = {
        "project_key": j["project_key"], "issue_type": j.get("issue_type", "Bug"), "parent": j.get("parent_epic"),
        "summary": full_title(d, cfg), "description_markdown": build_markdown(d, cfg),
        "priority": d["priority"], "labels": j.get("labels", []) + [script_label(d["script"])],
        "custom_fields_by_name": {j["tester_region_field"]["name"]: d.get("region") or j["tester_region_field"]["value"]} if j.get("tester_region_field") else {},
        "attachments": [str((ws / p).resolve()) for p in d["evidence"]],
        "defect_id": d["id"],
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def cmd_mark_filed(ws, cfg, a):
    reg = load_reg(ws)
    d = get(reg, a.id)
    d.update(status="filed", jira_key=a.key.upper(), filed_at=now_iso())
    save_json(reg_path(ws), reg)
    refresh_outputs(ws, cfg, reg, [d])
    print("%s -> %s" % (d["id"], d["jira_key"]))


def md_to_jira(md):
    """Markdown from build_markdown -> Jira wiki markup (what the Jira CSV importer renders)."""
    out, in_code = [], False
    for line in md.split("\n"):
        if line.strip().startswith("```"):
            out.append("{code}" if not in_code else "{code}")
            in_code = not in_code
            continue
        if in_code:
            out.append(line)
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            line = "h%d. %s" % (len(m.group(1)), m.group(2))
        elif line.strip() == "---":
            line = "----"
        elif re.match(r"^\d+\.\s", line):
            line = re.sub(r"^\d+\.\s", "# ", line)
        elif line.startswith("- "):
            line = "* " + line[2:]
        line = re.sub(r"\*\*(.+?)\*\*", r"*\1*", line)
        out.append(line)
    return "\n".join(out)


def cmd_csv(ws, cfg, a):
    reg = load_reg(ws)
    j = cfg["jira"]
    ds = [d for d in reg["defects"].values() if d["status"] == "approved" and any(o["run"] == a.run for o in d["occurrences"])]
    out = ws / "defects" / a.run / "jira_import.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Summary", "Issue Type", "Priority", "Description", "Labels", "Labels", "Labels", "Labels", "Parent",
                    j["tester_region_field"]["name"], "UAT Defect ID"])
        for d in ds:
            labels = (j.get("labels", []) + [d["script"]] + ["", "", "", ""])[:4]
            w.writerow([full_title(d, cfg), j.get("issue_type", "Bug"), d["priority"], md_to_jira(build_markdown(d, cfg)), *labels,
                        j.get("parent_epic", ""), d.get("region") or j["tester_region_field"]["value"], d["id"]])
    print("Wrote %d approved defect(s) to %s (attach screenshots manually after import)" % (len(ds), out))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ws")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("draft"); p.add_argument("--run", required=True)
    p = sub.add_parser("list"); p.add_argument("--status"); p.add_argument("--run")
    p = sub.add_parser("show"); p.add_argument("id")
    p = sub.add_parser("approve"); p.add_argument("ids", nargs="+")
    p = sub.add_parser("reject"); p.add_argument("ids", nargs="+"); p.add_argument("--reason", required=True)
    p = sub.add_parser("edit"); p.add_argument("id"); p.add_argument("--title"); p.add_argument("--severity")
    p.add_argument("--steps-file"); p.add_argument("--actual")
    p.add_argument("--category", help="FE | CMS | Content"); p.add_argument("--category-reason")
    p = sub.add_parser("jira-payload"); p.add_argument("id"); p.add_argument("--force", action="store_true")
    p = sub.add_parser("mark-filed"); p.add_argument("id"); p.add_argument("key")
    p = sub.add_parser("csv"); p.add_argument("--run", required=True)
    a = ap.parse_args()
    ws = find_workspace(a.ws)
    cfg = load_config(ws)
    if a.cmd == "approve":
        cmd_set_status(ws, cfg, a, "approved")
    elif a.cmd == "reject":
        cmd_set_status(ws, cfg, a, "rejected")
    else:
        {"draft": cmd_draft, "list": cmd_list, "show": cmd_show, "edit": cmd_edit, "jira-payload": cmd_payload,
         "mark-filed": cmd_mark_filed, "csv": cmd_csv}[a.cmd](ws, cfg, a)


if __name__ == "__main__":
    main()
