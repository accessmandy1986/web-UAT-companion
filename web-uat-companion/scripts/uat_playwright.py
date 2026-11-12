#!/usr/bin/env python3
"""Export PASSED UAT scripts to Playwright regression tests for the test lead.

Commands:
  scaffold                      create <ws>/playwright (package.json, config, helpers, README, .env.example)
  export --run RUN_ID [--scripts 001-016]
                                one spec per passed script+viewport -> playwright/tests/generated/
  env-local                     write playwright/.env from config/accounts.local.json (this machine only, gitignored)

Only script/viewport combinations whose steps ALL passed (n/a allowed) and that have recorded
`actions` are exported. Secrets and test-account emails become env-var placeholders, never literals.

Recorded action schema (written by the runner into each step's `actions`):
  {"type":"goto","url":"https://..."}
  {"type":"click"|"hover"|"check","locator":L}
  {"type":"fill","locator":L,"value":"text or {{gold.email}} / {{gold.password}} / {{SITE_PASSWORD}}"}
  {"type":"press","key":"Enter","locator":L?}     {"type":"select","locator":L,"value":"USD"}
  {"type":"expect_visible"|"expect_hidden","locator":L}
  {"type":"expect_text","locator":L?,"text":"..."}  {"type":"expect_url","pattern":"/en/..."}
  {"type":"wait_ms","ms":500}                       {"type":"note","text":"manual check: ..."}
  L = {"role":"button","name":"Log In"} | {"label":..} | {"placeholder":..} | {"text":..} | {"testid":..} | {"css":..}
      optional "exact":true, "nth":0, "within":L
"""
import argparse
import json
import re
from pathlib import Path

from uat_common import die, expand_selection, find_workspace, load_config, load_json, now_iso, script_key, script_num

PH = re.compile(r"\{\{\s*([A-Za-z0-9_]+)(?:\.([A-Za-z0-9_]+))?\s*\}\}")


def env_name(k, f):
    return ("UAT_%s" % k if not f else "UAT_%s_%s" % (k, f)).upper()


def js(s):
    return json.dumps(s, ensure_ascii=False)


def value_expr(v, used):
    """'{{gold.email}}' -> env('UAT_GOLD_EMAIL'); mixed strings become template literals."""
    v = str(v)
    parts, last = [], 0
    for m in PH.finditer(v):
        if m.start() > last:
            parts.append(("lit", v[last:m.start()]))
        name = env_name(m.group(1), m.group(2))
        used.add(name)
        parts.append(("env", name))
        last = m.end()
    if last < len(v):
        parts.append(("lit", v[last:]))
    if not parts:
        return js("")
    if len(parts) == 1:
        return js(parts[0][1]) if parts[0][0] == "lit" else "env(%s)" % js(parts[0][1])
    return " + ".join(js(p) if t == "lit" else "env(%s)" % js(p) for t, p in parts)


def loc_expr(L, base="page"):
    if not L:
        return base
    if L.get("within"):
        base = loc_expr(L["within"], base)
    ex = ", exact: true" if L.get("exact") else ""
    if "role" in L:
        e = "%s.getByRole(%s%s)" % (base, js(L["role"]), (", { name: %s%s }" % (js(L["name"]), ex)) if L.get("name") else "")
    elif "label" in L:
        e = "%s.getByLabel(%s%s)" % (base, js(L["label"]), ", { exact: true }" if ex else "")
    elif "placeholder" in L:
        e = "%s.getByPlaceholder(%s)" % (base, js(L["placeholder"]))
    elif "testid" in L:
        e = "%s.getByTestId(%s)" % (base, js(L["testid"]))
    elif "text" in L:
        e = "%s.getByText(%s%s)" % (base, js(L["text"]), ", { exact: true }" if ex else "")
    elif "css" in L:
        e = "%s.locator(%s)" % (base, js(L["css"]))
    else:
        raise ValueError("bad locator %r" % L)
    if "nth" in L:
        e += ".nth(%d)" % int(L["nth"])
    return e


def action_lines(a, used):
    t = a.get("type")
    L = a.get("locator")
    if t == "goto":
        return ["await page.goto(%s);" % value_expr(a["url"], used), "await passSiteGate(page);", "await page.waitForLoadState(\"load\"); // let the page hydrate before interacting"]
    if t in ("click", "hover", "check"):
        return ["await %s.%s();" % (loc_expr(L), t)]
    if t == "fill":
        return ["await %s.fill(%s);" % (loc_expr(L), value_expr(a.get("value", ""), used))]
    if t == "press":
        return ["await %s.press(%s);" % (loc_expr(L) if L else "page.keyboard", js(a["key"]))]
    if t == "select":
        return ["await %s.selectOption(%s);" % (loc_expr(L), value_expr(a["value"], used))]
    if t == "expect_visible":
        return ["await expect(%s).toBeVisible();" % loc_expr(L)]
    if t == "expect_hidden":
        return ["await expect(%s).toBeHidden();" % loc_expr(L)]
    if t == "expect_text":
        return ["await expect(%s).toContainText(%s);" % (loc_expr(L) if L else "page.locator('body')", js(a["text"]))]
    if t == "expect_url":
        return ["await expect(page).toHaveURL(new RegExp(%s));" % js(re.escape(a["pattern"]) if not a.get("regex") else a["pattern"])]
    if t == "wait_ms":
        return ["await page.waitForTimeout(%d);" % int(a.get("ms", 500))]
    if t == "note":
        return ["// MANUAL: %s" % a.get("text", "").replace("\n", " ")]
    return ["// unsupported recorded action: %s" % json.dumps(a)]


def literal_to_placeholders(ws, obj):
    acc = load_json(ws / "config" / "accounts.local.json", default={})
    m = {}
    if acc.get("site_gate", {}).get("password"):
        m[acc["site_gate"]["password"]] = "{{SITE_PASSWORD}}"
    for k, v in (acc.get("accounts") or {}).items():
        for f in ("password", "email"):
            if v.get(f):
                m.setdefault(v[f], "{{%s.%s}}" % (k, f))
    s = json.dumps(obj, ensure_ascii=False)
    for lit, ph in sorted(m.items(), key=lambda x: -len(x[0])):
        s = s.replace(json.dumps(lit)[1:-1], ph)
    return json.loads(s)


def cmd_export(ws, cfg, a):
    run = load_json(ws / "runs" / a.run / "run.json")
    tests = {s["id"]: s for s in load_json(ws / "tests" / "tests.json")["scripts"]}
    pw = ws / "playwright"
    if not (pw / "package.json").exists():
        cmd_scaffold(ws, cfg, a)
    out = pw / "tests" / "generated"
    out.mkdir(parents=True, exist_ok=True)
    ids = expand_selection(a.scripts, run["scripts"]) if a.scripts else run["scripts"]
    exported, skipped = [], []
    used_env = set()
    for sid in ids:
        s = tests[sid]
        blocks = []
        for v in run["viewports"]:
            res = [run["results"]["%s|%s|%s" % (sid, st["step"], v)] for st in s["steps"]]
            st_set = {r["status"] for r in res}
            if not st_set <= {"pass", "not_applicable"} or "pass" not in st_set:
                skipped.append("%s/%s (%s)" % (sid, v, ",".join(sorted(st_set))))
                continue
            if not any(r.get("actions") for r in res):
                skipped.append("%s/%s (no recorded actions)" % (sid, v))
                continue
            lines = []
            for st, r in zip(s["steps"], res):
                lines.append("")
                lines.append("// Step %s: %s" % (st["step"], st["description"].replace("\n", " ")))
                lines.append("// Expected: %s" % (r.get("expected") or "").replace("\n", " ")[:300])
                for act in literal_to_placeholders(ws, r.get("actions") or []):
                    lines += action_lines(act, used_env)
            blocks.append((v, lines))
        if not blocks:
            continue
        body = ["// AUTO-GENERATED from %s (%s) on %s by web-uat-companion." % (a.run, run["environment"], now_iso()),
                "// Source: UAT workbook %s - %s" % (sid, s["scenario"].replace("\n", " ")),
                "// Review locators before adding to CI. Secrets come from playwright/.env (see .env.example).",
                "import { test, expect } from '@playwright/test';",
                "import { env, passSiteGate } from '../helpers/uat';", "",
                "test.describe(%s, () => {" % js("%s - %s" % (sid, s["scenario"].replace("\n", " ")[:100]))]
        for v, lines in blocks:
            body.append("  test(%s, async ({ page }, testInfo) => {" % js("%s [%s]" % (sid, v)))
            body.append("    test.skip(testInfo.project.name !== %s, 'viewport-specific test');" % js(v))
            body += ["    " + l if l else "" for l in lines]
            body.append("  });")
        body.append("});")
        f = out / ("%s.spec.ts" % sid.lower())
        f.write_text("\n".join(body) + "\n", encoding="utf-8")
        exported.append(f.name)
    write_env_example(pw, used_env)
    print("Exported %d spec file(s) to %s: %s" % (len(exported), out, ", ".join(exported) or "-"))
    if skipped:
        print("Not exported (not fully passed or no actions): %s" % "; ".join(skipped))


def write_env_example(pw, used):
    ex = pw / ".env.example"
    keys = set()
    if ex.exists():
        keys = {l.split("=")[0] for l in ex.read_text().splitlines() if "=" in l and not l.startswith("#")}
    keys |= used | {"UAT_BASE_URL", "UAT_SITE_PASSWORD"}
    ex.write_text("# Copy to .env and fill in. Never commit .env.\n" + "\n".join("%s=" % k for k in sorted(keys)) + "\n")


def cmd_env_local(ws, cfg, a):
    acc = load_json(ws / "config" / "accounts.local.json")
    lines = ["UAT_BASE_URL=%s" % cfg["base_url"]]
    if acc.get("site_gate", {}).get("password"):
        lines.append("UAT_SITE_PASSWORD=%s" % acc["site_gate"]["password"])
    for k, v in acc.get("accounts", {}).items():
        for f in ("email", "password"):
            if v.get(f):
                lines.append("%s=%s" % (env_name(k, f), v[f]))
    p = ws / "playwright" / ".env"
    p.write_text("\n".join(lines) + "\n")
    p.chmod(0o600)
    print("Wrote %s (local only; gitignored)" % p)


def cmd_scaffold(ws, cfg, a):
    pw = ws / "playwright"
    (pw / "tests" / "generated").mkdir(parents=True, exist_ok=True)
    (pw / "tests" / "helpers").mkdir(parents=True, exist_ok=True)
    vp = cfg["viewports"]
    files = {
        "package.json": json.dumps({
            "name": "uat-regression", "private": True, "version": "0.1.0",
            "scripts": {"test": "playwright test", "test:desktop": "playwright test --project=desktop",
                        "test:mobile": "playwright test --project=mobile", "report": "playwright show-report"},
            "devDependencies": {"@playwright/test": "1.49.1", "dotenv": "16.4.7"}}, indent=2) + "\n",
        "playwright.config.ts": """import { defineConfig, devices } from '@playwright/test';
import * as dotenv from 'dotenv';
dotenv.config();

export default defineConfig({
  testDir: './tests',
  timeout: 90_000,
  expect: { timeout: 10_000 },
  retries: process.env.CI ? 1 : 0,
  reporter: [['html', { open: 'never' }], ['list']],
  use: {
    baseURL: process.env.UAT_BASE_URL || %(base)s,
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    trace: 'retain-on-failure',
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'], viewport: { width: %(dw)d, height: %(dh)d } } },
    { name: 'mobile',  use: { ...devices['Pixel 7'], viewport: { width: %(mw)d, height: %(mh)d } } },
  ],
});
""" % dict(base=js(cfg["base_url"]), dw=vp["desktop"]["width"], dh=vp["desktop"]["height"],
           mw=vp["mobile"]["width"], mh=vp["mobile"]["height"]),
        "tests/helpers/uat.ts": """import { Page } from '@playwright/test';

/** Read a required env var (set in playwright/.env - see .env.example). */
export function env(name: string): string {
  const v = process.env[name];
  if (!v) throw new Error(`Missing env var ${name} - copy .env.example to .env and fill it in`);
  return v;
}

/** the UAT site is behind Vercel password protection ("Protected Page" + "Unlock"). Passes it if shown. */
export async function passSiteGate(page: Page): Promise<void> {
  const gate = page.getByRole('button', { name: 'Unlock' });
  if (await gate.isVisible({ timeout: 3000 }).catch(() => false)) {
    await page.getByPlaceholder(/enter password/i).fill(env('UAT_SITE_PASSWORD'));
    await gate.click();
    await page.waitForLoadState('domcontentloaded');
    await gate.waitFor({ state: 'hidden', timeout: 15_000 });
  }
}
""",
        "README.md": """# UAT - Playwright regression pack

Generated by the `web-uat-companion` Claude plugin from UAT scripts that **passed** in an automated run.
Each spec in `tests/generated/` maps 1:1 to a `Test-Script-NNN` in the UAT workbook and keeps the
step descriptions and expected results as comments.

## First-time setup (test lead)
```bash
cd playwright
npm install
npx playwright install chromium
cp .env.example .env   # fill in UAT_SITE_PASSWORD and the UAT_<ACCOUNT>_EMAIL/PASSWORD values
```
(On the machine that ran Claude, `python3 <plugin>/scripts/uat_playwright.py env-local` writes `.env` for you.)

## Run
```bash
npm test                 # all, desktop + mobile
npm run test:desktop
npx playwright test tests/generated/test-script-009.spec.ts --headed
npm run report
```

## Notes
- Locators were captured by Claude during the run (role/label/text first). Review them once and replace
  brittle ones with `data-testid`s if the dev team can add them.
- Lines starting `// MANUAL:` are checks that need human judgement and are not asserted.
- Re-export after a new UAT run to pick up newly passing scripts; files are overwritten per script.
""",
    }
    for rel, content in files.items():
        p = pw / rel
        if p.exists() and rel != "tests/helpers/uat.ts" and not getattr(a, "force", False):
            continue
        p.write_text(content, encoding="utf-8")
    write_env_example(pw, set())
    gi = pw / ".gitignore"
    gi.write_text(".env\nnode_modules/\ntest-results/\nplaywright-report/\n")
    print("Playwright project ready at %s" % pw)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ws")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scaffold"); p.add_argument("--force", action="store_true")
    p = sub.add_parser("export"); p.add_argument("--run", required=True); p.add_argument("--scripts")
    sub.add_parser("env-local")
    a = ap.parse_args()
    ws = find_workspace(a.ws)
    cfg = load_config(ws)
    {"scaffold": cmd_scaffold, "export": cmd_export, "env-local": cmd_env_local}[a.cmd](ws, cfg, a)


if __name__ == "__main__":
    main()
