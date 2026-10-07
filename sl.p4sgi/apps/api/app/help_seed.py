"""Built-in help articles for the p4sgi dashboard (plain language, for admins who are not security specialists).

Seed articles are read-only code. Admin-created and AI-drafted articles live in DATA_DIR/help/articles.json.
Statements here describe what the dashboard actually does; keep them true when behaviour changes.
"""

from __future__ import annotations

HELP_SEED: list[dict[str, object]] = [
    {"id": "start-here", "category": "Getting started", "title": "Start here: what this dashboard checks and what it never does",
     "tags": ["overview", "start", "read-only"],
     "body": "The Security audit panel reads reports that were already downloaded from Google Workspace (via GAM) and scores them. It never changes anything in Google by itself. "
             "When you ask for help or a fix, the dashboard only shows you the steps (a 'plan') or drafts an email; a person must run or send it. "
             "Scores are triage hints, not a verdict: always look at the evidence. If a table says a report is missing, run that report under 'GAM reports' first."},
    {"id": "risk-levels", "category": "Security audit", "title": "What CRITICAL, HIGH, MEDIUM, LOW, UNKNOWN and APPROVED mean",
     "tags": ["risk", "score", "levels"],
     "body": "CRITICAL (85+) and HIGH (60+) need a decision soon. MEDIUM (40+) should be scheduled. LOW is informational. "
             "UNKNOWN means the report did not contain the information needed (never read it as safe). "
             "APPROVED means a super-admin recorded that the app is known and wanted; it is shown last and is re-checked when the review date passes or the app asks for new permissions."},
    {"id": "oauth-app", "category": "Security audit", "title": "What is an OAuth app (a third-party app with access to Google accounts)?",
     "tags": ["oauth", "app", "token", "consent"],
     "body": "When someone clicks 'Sign in with Google' or 'Allow access' for an app (Canva, Thunderbird, a Mac's Mail app), Google gives that app a token. "
             "The token lets the app read or change the person's Google data within the permissions (scopes) they approved. "
             "An app scores high when it can read all email, all of Drive or the whole directory. High does not mean bad: Apple Mail legitimately needs full mail access. "
             "Ask: who installed it, do we still need it, and does it need that much access?"},
    {"id": "approve-app", "category": "Security audit", "title": "How to mark an app as known and approved",
     "tags": ["approve", "known apps", "allowlist"],
     "body": "Only a super-admin can approve. Use Admin assist on the app row and choose 'Approve this app', or call the approved-apps endpoint. Write a reason (who asked for it, why). "
             "Approval lasts 180 days by default, then the app is flagged again. If the app later requests different permissions the approval is cancelled automatically and the real risk level returns. "
             "Approval does not change anything in Google; it only changes how this dashboard labels the app."},
    {"id": "revoke-app", "category": "Security audit", "title": "How to remove an app's access (revoke a token)",
     "tags": ["revoke", "token", "gam"],
     "body": "Admin assist can prepare the exact GAM command: 'gam user <email> delete token clientid <id>'. Nothing runs automatically: copy it and run it yourself, first on one account. "
             "To block an app for the whole organisation use Google Admin console > Security > Access and data control > API controls. "
             "Do not revoke an app that the tablets or this platform depend on (for example the service account used for attendance publishing) without checking first."},
    {"id": "two-step", "category": "Security audit", "title": "NO_2SV and ADMIN_NO_2SV: accounts without 2-step verification",
     "tags": ["2sv", "mfa", "password", "no_2sv"],
     "body": "2-step verification (2SV) asks for a phone prompt or code after the password, so a stolen password alone is not enough. "
             "NO_2SV is MEDIUM; an administrator without it (ADMIN_NO_2SV) is HIGH because that account can change everything. "
             "Fix: ask the person to turn it on at myaccount.google.com/security, or enforce it by OU in Admin console > Security > Authentication > 2-step verification. "
             "Shared school mailboxes need a plan first (who holds the phone?) so staff are not locked out."},
    {"id": "never-logged-in", "category": "Security audit", "title": "NEVER_LOGGED_IN and INACTIVE accounts",
     "tags": ["inactive", "never logged in", "dormant", "offboard"],
     "body": "NEVER_LOGGED_IN: the account is over 30 days old and nobody has signed in. INACTIVE: no sign-in for 90 days. "
             "These accounts keep their access but nobody notices if they are misused. Check whether the person left or the school never received the credentials. "
             "If they left: suspend (reversible), sign out sessions, transfer Drive files, move to an archive OU. Admin assist prepares that plan."},
    {"id": "suspicious-logins", "category": "Security audit", "title": "SUSPICIOUS_LOGINS: repeated failed sign-ins",
     "tags": ["failed login", "brute force"],
     "body": "Five or more failed sign-ins in 7 days. Often a person forgetting a password on a shared tablet; occasionally someone guessing passwords. "
             "Contact the user, reset the password if in doubt, and make sure 2SV is on."},
    {"id": "device-flags", "category": "Security audit", "title": "Device flags explained: UNENCRYPTED, NO_PASSWORD, STALE_SYNC, OUTDATED_PATCH and others",
     "tags": ["device", "encryption", "tablet", "patch", "sync"],
     "body": "UNENCRYPTED: storage is not encrypted, so a lost device exposes its data. NO_PASSWORD: no screen lock. STALE_SYNC: the device has not contacted Google for over 30 days (policies and remote wipe may not apply). "
             "OUTDATED_PATCH: security patch older than a year. DEVELOPER_MODE / USB_DEBUGGING / UNKNOWN_SOURCES: settings that make tampering easier. COMPROMISED: Google says it is rooted or tampered with. "
             "Score = 100 minus the points of each flag; under 85 is WARNING, under 60 is CRITICAL. A Windows PC can show UNENCRYPTED simply because it is not enrolled in management, so check the machine."},
    {"id": "stale-device", "category": "Security audit", "title": "A device has not synced for months: what to do",
     "tags": ["stale", "lost", "wipe", "tablet"],
     "body": "Ask the school whether the device is still in use. In use: connect it to Wi-Fi and sign in so it syncs; turn on a screen lock and encryption. "
             "Lost or retired: block it and wipe the school account data from it (a wipe is irreversible, so confirm first). Admin assist prepares the GAM steps; you need the device's GAM resourceId."},
    {"id": "admin-assist", "category": "Admin assist", "title": "How Admin assist works and what it can and cannot do",
     "tags": ["admin assist", "ai", "gemini", "help"],
     "body": "Tick one or more rows and press Admin assist, or press Assist at the end of a row. The assistant explains the issue in plain language and offers next steps as buttons: "
             "approve an app, prepare a revoke or offboarding plan, draft an email to the person, or ask a follow-up. "
             "It can only act on the rows you selected, can only offer a fixed list of steps, and never runs anything: plans are text for you to copy and emails open in your mail app as a draft. "
             "Privacy: the AI is not sent email addresses, names or serial numbers. People are replaced by labels like U1, and the real names are put back only on your screen."},
    {"id": "ai-privacy", "category": "Admin assist", "title": "What information is sent to the AI (Gemini) and what is not",
     "tags": ["privacy", "gemini", "cloud", "pseudonymised"],
     "body": "Sent: app names and public client IDs, permission names, risk flags and counts, device model and OS, dates, and your question with email addresses replaced by labels. "
             "Not sent: email addresses, names, serial numbers, SIM or phone numbers, learner data, attendance names. "
             "If Gemini is not configured the local Ollama model on this server is used and nothing leaves the building. Use a paid or Vertex Gemini key, not a free consumer key, because free tiers may use prompts to improve the product."},
    {"id": "email-draft", "category": "Admin assist", "title": "Emailing a user from the dashboard",
     "tags": ["email", "draft", "mailto"],
     "body": "The dashboard has no mail server and sends nothing. 'Draft an email' opens a ready-written message in your own mail program (mailto link) and shows the text to copy. You review and press send."},
    {"id": "pia", "category": "Privacy (PIA)", "title": "What the Privacy compliance (PIA) panel measures",
     "tags": ["pia", "privacy", "dpo", "compliance"],
     "body": "It tracks the Sierra Leone ULID Privacy Impact Assessment (40 controls, 18 risks) against evidence, not against what the workbook claims. A Data Protection Officer email must be entered first. "
             "Only a few controls can be measured from Google data; the rest need someone to attest with a link to the evidence. The Data Protection Bill is a draft and not law yet, so this is voluntary alignment."},
    {"id": "pia-evidence", "category": "Privacy (PIA)", "title": "Why a control shows 'no data' or a low score although the workbook says it is done",
     "tags": ["pia", "evidence", "attest", "declared"],
     "body": "The workbook status is a claim ('declared'). The dashboard scores only what it can measure or what someone has evidenced. An attestation with a link to the document counts about twice as much as one without. "
             "Evidence expires after a year. 'No data' is not a failure; it means nobody has shown evidence yet."},
    {"id": "ask-data", "category": "Ask the data", "title": "Asking questions about the database in plain language",
     "tags": ["chat", "sql", "ollama", "pgvector"],
     "body": "The Ask the data panel turns a question into one read-only query over curated views (schools, tablets, attendance counts, jobs). It cannot see names, serials or phone numbers. "
             "Always read the SQL shown under the answer. Questions about Google Workspace users or logins belong in the Security audit panel."},
    {"id": "glossary", "category": "Glossary", "title": "Glossary: GAM, OU, scope, token, service account, DPO",
     "tags": ["glossary", "gam", "ou", "scope", "service account", "dpo"],
     "body": "GAM: the command-line tool that reads (and can change) Google Workspace. OU: organisational unit, a folder of accounts in the Admin console. Scope: one specific permission an app asks for. "
             "Token: the pass an app holds after someone approves it. Service account: a robot account an application uses, with its own ID. DPO: Data Protection Officer or focal point."},
]
