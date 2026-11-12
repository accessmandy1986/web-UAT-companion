---
name: uat-playwright
description: Export UAT scripts that fully passed in an automated run into a Playwright regression pack (playwright/ sub-folder of the UAT workspace) for the test lead to re-run without Claude. Use when the user asks to "export passed tests to Playwright", "build regression scripts", or after a /uat-run with passing scripts.
---

# Playwright export for passed UAT scripts

Script: `<plugin>/scripts/uat_playwright.py` (`<plugin>` = two levels up from this skill's base directory).

1. `python3 uat_playwright.py --ws WS export --run RUN_ID [--scripts 009-016]`
   - Only script+viewport combinations where every step passed and recorded `actions` exist are exported.
   - Output: `<workspace>/playwright/tests/generated/test-script-NNN.spec.ts` (one test per viewport,
     step descriptions and expected results kept as comments). Account emails, passwords and the site password
     are env placeholders (`env('UAT_GOLD_PASSWORD')`), never literals.
   - First export also scaffolds `package.json`, `playwright.config.ts` (desktop + mobile projects),
     `tests/helpers/uat.ts`, `.env.example`, `README.md`.
2. Optionally validate: `cd <workspace>/playwright && npm install && npx playwright install chromium && npx playwright test --project=desktop <spec>`.
   If a generated locator is wrong, fix the spec by hand and tell the user which locators were adjusted.
3. Tell the user which scripts were exported and which were skipped (and why), and point the test lead at
   `playwright/README.md`. `env-local` writes a `.env` from the local accounts file on this machine only.
