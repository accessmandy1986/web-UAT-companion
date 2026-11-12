---
name: uat-parse
description: Read the UAT Excel workbook (pilot layout with Test Plan + Test Script sheets, or the External UAT Cycle 2 layout with UAT2-NNN-NN scenario and web script tabs) and convert every test script into structured JSON for automated execution. Use when the user says "parse/load/import the UAT test cases", a new version of the UAT workbook arrives, or before /uat-run if tests/tests.json is missing or stale.
---

# UAT workbook parser

Scripts live in `../../scripts/` relative to this skill's base directory. Call them as
`python3 <plugin>/scripts/uat_parse.py`. Requires `openpyxl` (`pip3 install --user openpyxl`).

## Steps
1. Locate the workspace: a folder with `config/uat.config.json` (usually `UAT-Automation/uat-workspace`).
   If none exists, copy `templates/workspace/` from the plugin, ask the user for the workbook path,
   base URL and Jira settings, and fill `config/uat.config.json`.
2. If the user supplied a new workbook, update `source_workbook` and the sheet names in `config/uat.config.json`.
   Pick the layout: `"layout": "uat2"` with `sheets.scenarios` + `sheets.scripts` (e.g. "5.2 UAT2 R1 Test Scenarios"
   and "6.1 UAT2 R1 WEB Test Scripts"; for Release 2 use the 5.3 R2 tab and its web scripts tab), or no `layout`
   (pilot) with `sheets.plan` + `sheets.scripts`. Check the scenario tab actually has rows - an empty tab parses to
   "no scenario rows" and every script is reported unmatched.
3. Run `python3 <plugin>/scripts/uat_parse.py --ws <workspace>`.
4. Read `<workspace>/tests/parse_report.md` and tell the user, briefly:
   - scripts and steps parsed, warnings
   - IDs recovered from broken `#REF!` cells (the table lets the test lead spot-check the mapping)
   - how many expected results are vague ("applicable", "correctly"...) - these produce lower-confidence verdicts
   - scripts with human-only steps (payment, social login, OTP, captcha, biometric, native app)
   - UAT2 only: per-region current web status (AMEA / AUNZ / EUAM) and scenarios not applicable to a region

## How the workbook is read
- Plan sheet: one row per `Test-Script-NNN` with Journey, Scenario and `Applicable To` Web / Web Resp / App flags.
- Script sheet: blocks stacked vertically, each starting with `Test Script ID:`, then `Test Scenario:`,
  `Test Component:`, a `Step` header row and a legend row. Per-platform column groups
  (Desktop, Webview, iOS, Android) each have Expected / Actual / Pass-Fail / Defect ID / Tester / Date / Remarks.
- When a block's ID is `#REF!`, it is assigned the next unclaimed plan ID in order (`id_source: inferred_by_order`).
- Never modify the source workbook. Results are written to a copy per run.

### UAT2 layout (External UAT Cycle 2 workbook, `"layout": "uat2"`)
- Scenario tab: header row with `AMEA | AUNZ | EUAM` applicability (Yes/No), Variation, Test Case ID, Sub-Test Case ID,
  Journey, Sub-Journey, Functionality, Test Case, User Story, Applicable to Web / App, then per-region
  `Test Status` + `Test Evidence` columns under WEB TEST STATUS (and APP TEST STATUS, ignored).
- Scripts tab: blocks labelled in column B - `Test Case ID:` (+ Positive/Negative), `Sub Test Case ID:` (+ Logged-In/
  Non Logged-In), `Test Component:`, `Test Case:`, `User Story:`, `Pre-Test Criteria:`, then `Step | Step Description |
  Expected Result`. Script ID = Sub Test Case ID (or Test Case ID when there is no sub ID, e.g. `UAT2-011`).
- One expected result serves desktop and mobile. There are no per-step result columns: results go to the region's
  status/evidence cells on the scenario tab plus an added `Automated step results` sheet in the run's workbook copy.
