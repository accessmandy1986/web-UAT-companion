---
name: uat-defects
description: Turn failed steps from an automated UAT run into Jira bug drafts in the UAT defect template ([Web] [FE|CMS|Content] - title, Steps to Replicate, Actual Results, screenshots, technical details), let a human approve them, then file approved ones in Jira (project PROJ, parent PROJ-100) with screenshots. Use after /uat-run, or when the user says "draft/approve/file the defects", "raise Jira tickets for the failures".
---

# UAT defect drafting, review and Jira filing

Scripts: `<plugin>/scripts/uat_defects.py` (`<plugin>` = two levels up from this skill's base directory).
Template and severity rubric: `<plugin>/templates/jira_defect_template.md`.

## 1. Draft
`python3 uat_defects.py --ws WS draft --run RUN_ID`
- One draft per failing script+step; desktop and mobile failures of the same issue are merged (`[Web/Web Resp]`).
- Duplicates of earlier defects are matched by fingerprint and not drafted again; if the earlier one is already
  filed it is flagged "recurring" (suggest a Jira comment instead of a new ticket).
- Then `python3 uat_results.py --ws WS export-xlsx --run RUN_ID` so draft IDs appear in the workbook's Defect ID column.
- Read each draft (`show DEF-xxxx`). Improve weak ones before review with `edit`:
  title must describe the user-visible problem in under 90 characters; repro steps must be followable from the home page.
  Check the type tag on every draft: `[FE]` front-end, likely code change; `[CMS]` Sitecore, likely code change;
  `[Content]` editable text/image, no code change. Fix with `edit DEF-xxxx --category CMS --category-reason "..."`.

## 2. Human review (required)
Tell the user the review page path `defects/RUN_ID/review.html` (offer to open it). They tick genuine defects and
paste back the approval command, or tell you in chat. Then:
`python3 uat_defects.py --ws WS approve DEF-0001 DEF-0003`
`python3 uat_defects.py --ws WS reject DEF-0002 --reason "intended behaviour"`
Do not file anything that is not `approved`. Approval is per defect; never assume.

## 3. File in Jira (Atlassian connector)
For each approved defect:
1. `python3 uat_defects.py --ws WS jira-payload DEF-xxxx` -> JSON with summary, markdown description,
   project, issue type, parent epic, priority, labels, "Tester region" = the run's region (AMEA, AUNZ or EUAM; config default otherwise), attachment paths.
2. Create the issue with the Atlassian (Rovo) connector's Jira create-issue tool. Use the cloud ID / site the connector
   reports; set parent to the epic; set priority and labels if the project allows; map "Tester region" by
   looking up the field (it is a custom field - fetch create metadata for PROJ Bug to find its id/allowed value).
   If the connector is not authorised, tell the user to authorise it via /mcp, and offer the CSV fallback.
3. Attachments: if the connector supports attachments, attach every file in `attachments`. Otherwise run
   `python3 <plugin>/scripts/jira_attach.py PROJ-1234 <files...>` - this needs `JIRA_BASE_URL`, `JIRA_EMAIL`,
   `JIRA_API_TOKEN` env vars that the **user sets themselves**; never ask for or handle the token.
4. `python3 uat_defects.py --ws WS mark-filed DEF-xxxx PROJ-1234`
5. When all are filed: `python3 uat_results.py --ws WS export-xlsx --run RUN_ID` so the Jira keys land in the
   workbook's Defect ID column.
6. Report the created keys with links.

## Fallback: CSV import
`python3 uat_defects.py --ws WS csv --run RUN_ID` -> `defects/RUN_ID/jira_import.csv` for Jira's external system
import (screenshots then attached manually or with jira_attach.py).

## Rules
- Ticket content never contains passwords (they are scrubbed to placeholders; double-check before filing).
- Low-confidence drafts carry a warning on the review page; recommend the reviewer reproduces them first.
- Keep the team's template order: heading "UAT Cycle 2 Functional Defect", Steps to Replicate the Issue,
  Actual Results, (Expected Results), Attach the screenshot, then Technical details.
