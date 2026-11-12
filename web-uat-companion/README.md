# web-uat-companion (Claude plugin)

Automated UAT for a website (configure the URL in `config/uat.config.json`). Claude reads the UAT Excel workbook, carries out each test
script in Chrome (desktop and mobile), records evidence, writes results back to Excel, drafts Jira bugs in the
UAT defect template for human approval, and exports passing scripts to Playwright for the test lead.

| Skill | What it does |
|---|---|
| `/uat-parse` | Reads `UAT Test Cases.xlsx` (plan + test script sheets) into `tests/*.json`; repairs `#REF!` IDs; flags vague expected results and human-only steps |
| `/uat-run 001-016` | Runs the scripts with Claude in Chrome at 1440x900 and 390x844; screenshot per step, GIF on failure, console + network errors; Pass / Fail / Blocked / Needs human; HTML report + results workbook |
| `/uat-defects` | Drafts one Jira bug per issue (merged across viewports, de-duplicated across runs); review page with approve tick-boxes; files approved bugs to PROJ under PROJ-100 with screenshots |
| `/uat-playwright` | Turns fully-passed scripts into `playwright/tests/generated/*.spec.ts` for regression |

## Install (each tester)
Requirements: Claude Code (desktop app or CLI), the **Claude in Chrome** extension connected, Python 3.9+ with
`pip3 install --user openpyxl`. For Jira filing: the **Atlassian (Rovo)** connector authorised (`/mcp`).

```
/plugin marketplace add accessmandy1986/web-UAT-companion
/plugin install web-uat-companion@web-uat-companion
```

## Workspace (per tester, not shared)
Copy `templates/workspace/` to e.g. `UAT-Automation/uat-workspace/`, then:
1. `config/uat.config.json` - workbook path, sheet names, base URL, Jira project/epic/region.
2. Copy `config/accounts.example.json` to `config/accounts.local.json` and fill in the site password and UAT test
   accounts. This file is gitignored and must never be shared, committed or pasted into tickets.

Folder layout after a run:
```
uat-workspace/
  config/            uat.config.json, accounts.local.json (secret)
  tests/             tests.json, Test-Script-NNN.json, parse_report.md
  runs/RUN-.../      run.json, report.html, evidence/, <workbook>_RESULTS_RUN-....xlsx
  defects/           registry.json, RUN-.../review.html, DEF-xxxx.md, jira_import.csv
  playwright/        regression pack for the test lead (see playwright/README.md)
```

## Typical session
1. "Parse the UAT workbook" -> check `tests/parse_report.md`.
2. "Run UAT for scripts 001-016 on desktop and mobile".
3. Open `defects/RUN-.../review.html`, tick genuine defects, paste the approval line back to Claude.
4. "File the approved defects in Jira".
5. "Export passed scripts to Playwright".

## What stays with a human
- Card entry and payment submission (Claude stops at the payment form and resumes after the tester pays).
- Social login, OTP / email verification, CAPTCHA, biometric, native iOS/Android app columns.
- Approving every Jira ticket before it is filed.
- Pure design/brand judgement steps (marked "Needs human").

## Known limits
- If the Chrome window cannot be resized (full-screen/maximised), mobile is tested in a 390px same-origin frame:
  responsive layout is accurate, but there is no touch / mobile user agent. Un-maximise Chrome for true resizing.
- Vague expected results ("applicable validation", "displays correctly") get `confidence: medium`; the review page
  flags them for a manual check.
- Writing results uses openpyxl on a **copy** of the workbook; the original is never modified. openpyxl may drop
  embedded images/shapes in that copy.
