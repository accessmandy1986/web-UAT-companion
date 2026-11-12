#!/usr/bin/env python3
"""Attach screenshots/GIFs to an existing Jira issue via the Jira Cloud REST API.

Used when the Atlassian connector cannot upload attachments. Credentials come ONLY from
environment variables that the tester sets themselves (never stored in this plugin):
  JIRA_BASE_URL   e.g. https://your-company.atlassian.net
  JIRA_EMAIL      the tester's Atlassian account email
  JIRA_API_TOKEN  created at https://id.atlassian.com/manage-profile/security/api-tokens

Usage: python3 jira_attach.py PROJ-1234 file1.png [file2.gif ...]
"""
import base64
import mimetypes
import os
import sys
import urllib.request
import uuid
from pathlib import Path


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    key, files = sys.argv[1], [Path(p) for p in sys.argv[2:]]
    base, email, token = (os.environ.get(k) for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"))
    if not (base and email and token):
        raise SystemExit("Set JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN first (see --help).")
    boundary = uuid.uuid4().hex
    body = b""
    for f in files:
        if not f.exists():
            print("skip (missing): %s" % f)
            continue
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        body += ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
                 % (boundary, f.name, ctype)).encode() + f.read_bytes() + b"\r\n"
    body += ("--%s--\r\n" % boundary).encode()
    req = urllib.request.Request(
        "%s/rest/api/3/issue/%s/attachments" % (base.rstrip("/"), key), data=body, method="POST",
        headers={"Authorization": "Basic " + base64.b64encode(("%s:%s" % (email, token)).encode()).decode(),
                 "X-Atlassian-Token": "no-check", "Content-Type": "multipart/form-data; boundary=" + boundary,
                 "Accept": "application/json"})
    with urllib.request.urlopen(req) as r:
        print("Attached %d file(s) to %s (HTTP %s)" % (len(files), key, r.status))


if __name__ == "__main__":
    main()
