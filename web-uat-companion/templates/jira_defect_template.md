# Jira defect template (UAT)

Mirrors the team's existing Jira bug layout (e.g. PROJ-123 under epic PROJ-100).
The first block is the team template, unchanged. The "Technical details" block is added
below it for developers and can be removed in `uat.config.json` if not wanted.

**Summary:** `{prefix} - {title}`  e.g. `[Web] - Selected non-member rate does not update to the member rate after login`
- `[Web]` desktop, `[Web Resp]` mobile/responsive, `[Web/Web Resp]` both.
- Title = what is wrong, in user terms, max ~90 chars. No "Bug:", no step numbers.

**Key details**
- Project: PROJ · Issue type: Bug · Parent: PROJ-100
- Tester region: AMEA
- Priority: from severity (Critical→Highest, High→High, Medium→Medium, Low→Low)
- Labels: UAT-Cycle-2, auto-uat, claude-tester

**Description**

```
## UAT Cycle 2 Functional Defect

**Steps to Replicate the Issue:**
1. ...
2. ...

---

**Actual Results:**
<what happened>

**Expected Results:**
<expected result from the test script>

**Attach the screenshot:**
<screenshot(s) attached to the ticket>

---

**Technical details (automated UAT)**
- Test script: Test-Script-NNN step N - <scenario>
- Environment: uat.example.com · Viewport: Desktop 1440x900 / Mobile 390x844 · Browser: Chrome
- Page URL: ...
- Test account tier: GOLD (never the password)
- Console errors / failed network requests: ...
- Severity: High · Detection confidence: high · Run: RUN-...
```

## Severity rubric
| Severity | Use when |
|---|---|
| Critical | Booking, payment, login or registration cannot be completed; data loss; wrong price charged; security/privacy exposure; site/page down |
| High | Core journey works only with a workaround; wrong prices/rates/member benefits shown; key component missing/broken; JS error breaks a feature |
| Medium | Non-core feature broken; wrong/missing validation message; layout broken but usable; wrong link target; translation missing |
| Low | Cosmetic: spacing, alignment, typo, minor visual inconsistency; console-only error with no visible impact |
