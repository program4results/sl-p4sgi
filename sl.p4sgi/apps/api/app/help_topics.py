"""Extended help topics (0.4.23). Plain language, for administrators who are not security specialists.

Read-only code, indexed into pgvector (source 'help') together with help_seed.HELP_SEED so Help and Ask-the-data can look
them up quickly. Statements describe how Google Workspace / this dashboard work; GAM commands are examples to check with
`gam help` first. Keep them true when behaviour changes.
"""

from __future__ import annotations


def _a(id_: str, cat: str, title: str, tags: str, body: str) -> dict[str, object]:
    return {"id": id_, "category": cat, "title": title, "tags": tags.split(","), "body": body}


HELP_TOPICS: list[dict[str, object]] = [
    # ---------------------------------------------------------------- Getting started
    _a("how-to-triage", "Getting started", "A simple routine: how to work through the Security audit in 15 minutes",
       "routine,triage,weekly,start",
       "1) Press Load / Reload. 2) Read the cards at the top: anything CRITICAL or HIGH first. 3) In the app table, look at apps with many users and wide permissions. "
       "4) In the user table, deal with administrators without 2-step verification first. 5) In the device table, look at CRITICAL devices that have not synced for months. "
       "6) For each row you do not understand, press Assist. 7) Write down what you decided (approve, remove, email) so the next person knows. Repeat weekly."),
    _a("what-is-gam", "Getting started", "What GAM is and why the dashboard shows 'cached report' data",
       "gam,reports,cache,csv",
       "GAM is a command-line tool that reads Google Workspace data. The dashboard does not talk to Google live: it reads CSV files produced by GAM reports that someone ran earlier. "
       "So the tables are only as fresh as the last report run ('As of' date in the message line). If a table says a report is missing, run that report in the GAM reports section, then press Load / Reload."),
    _a("domain-filter", "Getting started", "The Domain filter and why you may see fewer rows than a colleague",
       "domain,filter,scope,permissions",
       "Normal administrators only see accounts and devices of their own school domain; super-admins can see all domains. The Domain filter narrows the view further. "
       "If a row you expect is missing, check the filter, then check that you are signed in with the right account."),
    _a("who-can-do-what", "Getting started", "Who can do what: administrators and super-admins",
       "roles,super-admin,permissions",
       "Everyone with access can read tables, ask Admin assist for explanations, draft emails and search the help. Only super-admins can approve apps, prepare removal/suspend/wipe steps, write or approve help articles and change privacy settings. "
       "Super-admin accounts themselves are protected: the dashboard refuses to prepare actions against them."),
    _a("read-only-promise", "Getting started", "What 'read-only' means here",
       "read-only,safe,changes",
       "The dashboard reads cached reports and shows them. It never changes Google Workspace and never talks to DHIS2. Advice is text: commands to copy, emails to review. A person always does the change. "
       "The only things it saves are its own notes: approved apps, help articles, privacy evidence and audit logs."),
    # ---------------------------------------------------------------- Security audit: apps
    _a("scopes-explained", "Security audit", "Permissions (scopes) explained: Gmail, Drive, Calendar, Directory",
       "scope,gmail,drive,calendar,directory,permission",
       "Full Gmail (mail.google.com) lets an app read, send and delete all email: highest risk. Full Drive lets it read and change every file. "
       "'drive.file' only lets an app touch files it created or you opened with it: much lower risk. Calendar, contacts and basic profile are lower risk. "
       "Directory (admin.directory) scopes can list or change users and are dangerous in the wrong hands. The fewer and narrower the permissions, the safer."),
    _a("apple-mail-thunderbird", "Security audit", "Mail programs (Apple Mail, Thunderbird, Outlook) showing as CRITICAL",
       "mail,thunderbird,apple,macos,outlook,imap",
       "A mail program on a computer or phone needs full mailbox access to work, so it scores CRITICAL on permissions alone. That is expected. "
       "Check: is the person on your staff list, is it a school-approved device, and is the number of users small? If yes, a super-admin can approve the app. If you do not recognise it, ask the user and consider removing access."),
    _a("canva-style-apps", "Security audit", "Design and productivity apps (Canva, Zoom, Slack, Kahoot) asking for access",
       "canva,zoom,slack,kahoot,education apps",
       "Most education apps ask for sign-in plus limited Drive or Calendar access. Check what the school actually uses them for. "
       "A narrow permission like 'drive.file' or 'calendar.events' is usually fine. A request for full Drive or full Gmail from a design app is a reason to ask why. Approve apps the school really uses; remove the rest."),
    _a("service-account-client", "Security audit", "An app used by many accounts at once (service account or domain-wide delegation)",
       "service account,delegation,dwd,platform",
       "When one 'app' appears with dozens or hundreds of users, it is often a platform's own service account or a domain-wide delegation grant, not something each person installed. "
       "Do NOT revoke it before checking: Admin console > Security > Access and data control > API controls > Domain-wide delegation shows the client IDs and what they may do. "
       "If it is this platform or another system you rely on, approve it with a clear reason and review date."),
    _a("review-date", "Security audit", "Why an approval expires and what to do when it does",
       "approval,expiry,review,stale",
       "Approvals last 180 days by default because needs change: a person leaves, an app is replaced. When the date passes, or the app asks for different permissions, the row shows its real level again with a note. "
       "Re-read who uses it and why, then approve again with a fresh reason, or remove the app."),
    _a("block-app-org-wide", "Security audit", "Blocking or limiting an app for the whole organisation",
       "block,api controls,trust,allowlist",
       "Admin console > Security > Access and data control > API controls lets you mark apps as trusted, limited or blocked, and restrict which apps may use sensitive scopes. "
       "A common safe setting is to restrict Gmail and Drive scopes to apps you have approved. Test with a small group first."),
    _a("user-asks-about-app", "Security audit", "A user says 'I never installed that app'",
       "unknown app,user,investigation",
       "Ask when and on which device they signed in with Google. Check the app's last-seen date and other users of the same app. If several unrelated people have it, it may be a platform tool. "
       "If only that person has it and they do not recognise it, remove its access (Assist prepares the command), have them change their password and turn on 2-step verification."),
    # ---------------------------------------------------------------- accounts
    _a("admin-no-2sv", "Accounts and access", "An administrator without 2-step verification (the most urgent account issue)",
       "admin,2sv,mfa,urgent",
       "Administrator accounts can change everything, so a stolen password is a disaster. Ask them to turn on 2-step verification today (myaccount.google.com/security) and consider security keys for super-admins. "
       "Keep at least two super-admins and store recovery codes safely. Do not share one admin login between people."),
    _a("enforce-2sv", "Accounts and access", "Making 2-step verification compulsory for a group",
       "enforce,2sv,ou,policy",
       "Admin console > Security > Authentication > 2-step verification: choose an organisational unit, allow users to turn it on, then enforce from a date with a grace period. "
       "Start with administrators and teachers, then others. Plan for people without smartphones (security keys or backup codes) before enforcing."),
    _a("suspend-vs-delete", "Accounts and access", "Suspend or delete? Why suspend first",
       "suspend,delete,offboard,restore",
       "Suspending blocks sign-in but keeps the data and can be undone. Deleting removes the account after a short recovery window. For leavers: suspend, sign out sessions, transfer Drive files to a manager, wait a few weeks, then delete if nobody needs it. "
       "Never delete an account you have not checked for shared files, groups it owns or school-wide forms."),
    _a("offboarding-checklist", "Accounts and access", "Leaver checklist for school staff",
       "leaver,offboarding,checklist,transfer",
       "1) Confirm with the school head. 2) Suspend the account. 3) Sign out all sessions. 4) Remove app tokens. 5) Transfer Drive ownership to the successor. 6) Forward or hand over important email. "
       "7) Remove from groups and shared drives. 8) Collect the tablet and sign it out (or wipe school data). 9) Record the date and who approved it."),
    _a("shared-accounts", "Accounts and access", "Shared accounts (one login used by many people)",
       "shared,account,class,tablet",
       "Shared logins hide who did what and make 2-step verification awkward. If a school really needs a shared mailbox, use a Google Group or delegated access instead. For tablets used by several learners, use the school's managed profile and avoid sharing a staff login."),
    _a("password-reset", "Accounts and access", "Resetting a password safely",
       "password,reset,recover",
       "Verify the person (call them on a number you already have, or ask the school head) before resetting. Use a temporary password that must be changed at next sign-in. "
       "Never send a password and the username in the same message. After a reset, check 2-step verification is on."),
    _a("failed-logins-what-next", "Accounts and access", "Many failed sign-ins: what to check and do",
       "failed login,brute force,lockout",
       "Look at whether the failures are from one person forgetting a password (common on shared tablets) or hundreds across many accounts (attack). "
       "Reset the password, confirm 2-step verification, and check the Login activity report for unfamiliar countries. If an administrator account is involved, treat it as urgent."),
    _a("inactive-accounts-policy", "Accounts and access", "A sensible policy for inactive and never-used accounts",
       "inactive,dormant,policy,licence",
       "Example policy: accounts unused for 90 days are reviewed; unused for 180 days are suspended after asking the school; unused for a year are archived or deleted. "
       "Never-used accounts older than 30 days usually mean the credentials never reached the person. Write the policy down and tell schools before you act."),
    # ---------------------------------------------------------------- devices
    _a("device-score", "Devices and tablets", "How the device compliance score is worked out",
       "score,compliance,devices,points",
       "Each device starts at 100. Each problem subtracts points (for example compromised, unencrypted, no screen lock, outdated security patch, stale sync, developer mode, USB debugging, unknown sources). "
       "Below 85 is WARNING, below 60 is CRITICAL. It is a triage hint: a device marked CRITICAL for one serious flag may need more urgent action than one with many minor flags."),
    _a("encryption", "Devices and tablets", "Why device encryption matters and how to turn it on",
       "encryption,android,lost device",
       "An unencrypted tablet lets anyone who finds it read the stored data. Modern Android devices are encrypted by default once a screen lock is set. "
       "Ask the school to set a PIN or password; if encryption is still reported off, the device may be too old or not properly enrolled and may need replacing or re-enrolling."),
    _a("screen-lock", "Devices and tablets", "NO_PASSWORD: setting a screen lock on tablets",
       "screen lock,pin,password,tablet",
       "A device without a screen lock exposes everything on it. For learner tablets use a simple school PIN set by a policy in Google Admin console > Devices > Mobile & endpoints > Settings. "
       "Enforce a minimum PIN length and automatic lock after a short time."),
    _a("outdated-patch", "Devices and tablets", "OUTDATED_PATCH: old Android security updates",
       "patch,android,update,eol",
       "Devices that have not received a security update in a year miss fixes for known attacks. Connect them to Wi-Fi and check for system updates. "
       "If the manufacturer no longer provides updates, the device is end-of-life: keep it off sensitive tasks and plan replacement."),
    _a("developer-mode", "Devices and tablets", "DEVELOPER_MODE, USB_DEBUGGING, UNKNOWN_SOURCES",
       "developer,usb,adb,unknown sources,sideload",
       "These settings let someone install or control apps in ways the school cannot manage. They are often turned on by a technician during setup and never turned off. "
       "Turn them off in Settings; use device policy to block them. If they keep coming back, check who has physical access."),
    _a("compromised-device", "Devices and tablets", "A device reported as compromised (rooted or tampered with)",
       "compromised,root,tamper,incident",
       "Treat it as untrusted: stop using it for school accounts, sign the user out everywhere, change their password, and wipe school data from the device (Assist prepares the steps). "
       "Do not wipe before checking it is not the only copy of something important."),
    _a("lost-tablet", "Devices and tablets", "A tablet is lost or stolen: what to do within the hour",
       "lost,stolen,wipe,incident,tablet",
       "1) Record the device and who had it. 2) Sign the user out and change their password. 3) Block the device in Admin console > Devices (or prepare the steps with Assist). "
       "4) Wipe school account data (account wipe) so a finder cannot use it. 5) Tell the school head. 6) If learner data was on it, note it for the privacy log."),
    _a("wipe-types", "Devices and tablets", "Account wipe or full wipe?",
       "wipe,account wipe,factory reset",
       "An account wipe removes only the school account and its data; the device and personal items stay. A full wipe resets the device to factory state and cannot be undone. "
       "For a lost or stolen school-owned tablet, a full wipe is often right; for a staff member's own phone, use an account wipe. The dashboard prepares the account wipe by default."),
    _a("resource-id", "Devices and tablets", "Finding the GAM resourceId needed for a device wipe",
       "resourceid,gam,mobile,device id",
       "The dashboard shows the device ID from reports, but GAM actions need the device's resourceId. Run: gam print mobile query \"email:name@school\" fields resourceId,deviceId,model,status "
       "(check the syntax with gam help for your version). Copy the resourceId into the wipe command."),
    _a("stale-sync-reasons", "Devices and tablets", "Why devices stop syncing and how to bring them back",
       "stale,sync,wifi,offline,sleep",
       "Common reasons: no Wi-Fi or data at the school, the device is switched off in a cupboard, the user signed out, the battery is dead, or the account was suspended. "
       "Ask the school to charge it, connect it and open the Google app. If it never returns after a month, treat it as lost."),
    _a("tablet-fleet-registry", "Devices and tablets", "Difference between the device registry (tablets) and Google-managed devices",
       "registry,fleet,tablets,google devices",
       "The platform's own registry lists tablets that have registered with the app and report telemetry. The Security audit device table lists devices that Google Workspace knows about. "
       "A tablet can be in one list and not the other; reconcile them if numbers disagree."),
    # ---------------------------------------------------------------- Workspace admin
    _a("admin-console-map", "Google Workspace admin", "Where to find things in the Google Admin console",
       "admin console,menu,navigation",
       "Users: Directory > Users. Groups: Directory > Groups. Devices: Devices > Mobile & endpoints. 2-step verification and API controls: Security. Reports and audit logs: Reporting. "
       "Domain-wide delegation: Security > Access and data control > API controls. Alerts: Security > Alert center."),
    _a("alert-center", "Google Workspace admin", "Using the Alert center",
       "alert,center,notifications",
       "The Alert center lists Google's own warnings (suspicious sign-ins, leaked passwords, device compromise). Set it to email the right administrators. "
       "Check it weekly together with this dashboard: Google sees live events, the dashboard sees yesterday's reports."),
    _a("groups-hygiene", "Google Workspace admin", "Groups and shared access hygiene",
       "groups,shared drive,access,hygiene",
       "Prefer Google Groups for access to shared drives and mailboxes. Remove leavers from groups, make sure each group has two owners, and avoid 'anyone on the internet' joining. Review group membership each term."),
    _a("drive-sharing", "Google Workspace admin", "Files shared too widely in Drive",
       "drive,sharing,public,link,exposure",
       "The dashboard cannot yet show Drive sharing exposure (it needs an extra read-only report). In the Admin console use Reporting > Audit and investigation > Drive log events to find files shared by link or with outside people, and set the sharing default for each organisational unit. "
       "Student and learner data should never be 'anyone with the link'."),
    _a("audit-logs", "Google Workspace admin", "Audit logs: who did what and when",
       "audit,log,investigation,reporting",
       "Admin console > Reporting > Audit and investigation has logs for sign-ins, Drive, admin actions and tokens. They are kept for a limited time, so export important evidence quickly. "
       "The dashboard keeps its own small audit trail of approvals, plans and assist sessions in its data folder."),
    _a("api-access-risk", "Google Workspace admin", "Why 'API access' should be restricted",
       "api,oauth,restrict,trust",
       "By default users can grant almost any app access to their data. Restricting API access means only trusted apps can use sensitive permissions. This single setting prevents most app-related incidents. Roll it out with an approved-apps list prepared from this dashboard."),
    # ---------------------------------------------------------------- incidents
    _a("suspected-compromise", "Incident response", "An account looks hacked: first steps",
       "hacked,compromised,incident,phishing",
       "1) Reset the password and sign out all sessions. 2) Turn on 2-step verification. 3) Remove unknown app tokens. 4) Check forwarding rules and delegates in Gmail settings. "
       "5) Look at recent sign-ins for unfamiliar places. 6) Warn the people they emailed. 7) Write down the time line. Escalate to a super-admin early."),
    _a("phishing-report", "Incident response", "Phishing emails: what to tell staff",
       "phishing,scam,email,awareness",
       "Tell staff: Google, the Ministry or IT will never ask for your password by email. Do not click links asking you to sign in. Report suspicious mail with the 'Report phishing' option, and tell the administrator if you already clicked or entered a password."),
    _a("data-breach-steps", "Incident response", "A possible personal-data breach: what to record",
       "breach,personal data,incident,notify",
       "Note what happened, when, which system, whose data (learners, teachers), how many people, what you did, and who you told. Contain it first (reset, block, wipe). "
       "Tell the data protection focal point (DPO) the same day. Whether and whom to notify depends on the law in force; the privacy panel lists the draft Bill's expectations."),
    _a("contact-user-tips", "Communication", "How to write to a worried or non-technical user",
       "email,message,tone,communication",
       "Use short sentences, say why you are writing, say exactly what you want them to do, and say what happens if they do nothing. Never ask for their password. Offer a phone number or a person to visit. "
       "Admin assist drafts messages like this; always read and edit them before sending."),
    _a("email-sending", "Communication", "How email from the dashboard works (mail program or SMTP)",
       "email,smtp,mailto,send",
       "By default the dashboard opens a draft in your own mail program, so the message leaves from your own address and you can edit it. "
       "If the server administrator has configured SMTP, a super-admin can also send the draft from the dashboard to the people on the selected rows. Recipients are limited to the people on those rows, and every send is logged."),
    # ---------------------------------------------------------------- privacy / data
    _a("learner-data", "Privacy (PIA)", "Learner data: the golden rules",
       "learner,student,children,data,privacy",
       "Collect only what is needed, keep it only as long as needed, restrict access to people who need it, never put learner names in chats or public documents, and never share lists by personal email. "
       "Photos and attendance for children need extra care. The AI helpers in this dashboard do not receive learner data."),
    _a("pseudonymisation", "Privacy (PIA)", "What pseudonymised means (U1, A1, D1)",
       "pseudonymise,labels,anonymous,ai",
       "Before anything is sent to an AI model, real names and emails are swapped for labels (U1 for a user, A1 for an app, D1 for a device). The model works with labels; the real names are put back only on your screen. "
       "This reduces risk but is not perfect anonymity: write free-text questions without personal details."),
    _a("ai-limits", "Admin assist", "What the AI can get wrong",
       "ai,limits,accuracy,hallucination",
       "AI can sound sure and be wrong. Treat explanations as a starting point, check the facts shown in the table, and test any command on one account first. "
       "The assistant cannot see your Google data live, only the cached report rows you selected."),
    _a("speed-tips", "Admin assist", "Why answers can be slow and how to speed them up",
       "slow,speed,model,ollama,performance",
       "The first reply appears instantly from the built-in explanation; the AI then improves it in the background. A large local model (14B) can take up to a minute on a PC. "
       "To speed up: set ASSIST_MODEL to a smaller model (for example a 3B or 7B), keep Ollama running with a GPU, or use Gemini. Searching the help guides is instant and does not need the AI."),
    _a("sample-questions", "Admin assist", "Questions you can ask Admin assist",
       "samples,prompts,questions,examples",
       "Apps: 'Is this app safe for a school?', 'Who uses it and do they still need it?', 'What happens if I remove its access?', 'Write an email asking the users if they need it.' "
       "Users: 'Why is this account flagged?', 'What is the safest next step?', 'Draft an email asking them to turn on 2-step verification.' "
       "Devices: 'Is this device lost?', 'Should we wipe it?', 'What should the school do?'"),
    # ---------------------------------------------------------------- dashboard how-to
    _a("howto-approve", "Dashboard how-to", "Approve a known app in three clicks",
       "approve,howto,known app",
       "Press Assist at the end of the app row, choose 'It is a known, wanted app: approve it', write a reason (who needs it and why) and confirm. The row turns APPROVED and moves to the bottom. "
       "Super-admin only. To undo, use Remove approval in the Known & approved apps list."),
    _a("howto-select", "Dashboard how-to", "Selecting many rows and asking for help at once",
       "select,all,none,batch,assist",
       "Tick the boxes, or use Select all / Select none above the table, then press the green AIdmin button (above the table, or in the section header next to Reload). Up to 25 rows are handled in one conversation. "
       "Use this to ask 'which of these should I look at first?' or to draft one email to all users of an app."),
    _a("howto-reports", "Dashboard how-to", "Which GAM reports feed which table",
       "reports,token_activity,users_full,login_activity,devices",
       "OAuth apps come from token_activity (needs the scope column). Users come from users_full, login_activity and the admins list. Devices come from the mobile devices report. "
       "If a table is empty or says UNKNOWN, run the missing report in the GAM reports section and reload."),
    _a("howto-help", "Dashboard how-to", "Using Help & guides and adding to it",
       "help,guides,search,questions",
       "Search or ask in plain words. Answers come first from the saved guides, found by meaning (pgvector) when the database is set up. Every question you ask is saved without personal details as a draft; a super-admin reviews drafts and approves useful ones so everyone benefits."),
    _a("troubleshoot-empty", "Troubleshooting", "A table is empty or shows 'not cached'",
       "empty,not cached,missing,report",
       "The report behind it has not been run, or ran but returned nothing for your domain. Check the message line for 'Missing cached reports', run those under GAM reports, then press Load / Reload. "
       "If you are not a super-admin, also check your domain scope."),
    _a("troubleshoot-ai", "Troubleshooting", "The AI does not answer or says 'No AI model answered'",
       "ollama,gemini,error,unreachable,model",
       "Check Ask the data > Check status: Ollama must be reachable from the container (OLLAMA_HOST=0.0.0.0, port 11434) and the model must be installed (ollama list). "
       "For Gemini check CHAT_CLOUD_ENABLED, GEMINI_API_KEY and GEMINI_MODEL, then restart the stack. The built-in explanation still works without any AI."),
    _a("troubleshoot-pgvector", "Troubleshooting", "Help search says 'full-text' instead of 'meaning'",
       "pgvector,embeddings,nomic,index",
       "Meaning search needs the embedding model (ollama pull nomic-embed-text) and the pgvector extension. Press 'Load help into pgvector' in Help & guides (super-admin). "
       "Without them the search still works with keywords."),
    # ---------------------------------------------------------------- glossary
    _a("glossary-2", "Glossary", "Glossary: OAuth, token, MFA, 2SV, delegation, MDM, EOL",
       "glossary,oauth,mfa,mdm,eol,delegation",
       "OAuth: the system that lets an app act for you without your password. Token: the pass the app keeps. MFA/2SV: a second proof besides the password. "
       "Delegation: giving a robot or a person power to act as others. MDM: mobile device management, the rules the school applies to tablets. EOL: end of life, no more security updates."),
    _a("glossary-3", "Glossary", "Glossary: encryption, patch, sideloading, wipe, super-admin, OU",
       "glossary,encryption,patch,sideload,wipe,ou",
       "Encryption: scrambling stored data so it is unreadable without the screen lock. Patch: a security update. Sideloading: installing apps from outside the official store. "
       "Wipe: erasing data remotely. Super-admin: the highest Google Workspace administrator. OU: an organisational unit, a folder of accounts used to apply different settings."),
]

HELP_TOPICS += [
    _a("rustadmin-what", "Remote support", "What RUSTAiDMIN is and what it does not do",
       "rustdesk,remote,desktop,adb,tablet,support",
       "RUSTAiDMIN is a section near the top of the dashboard. It shows the settings tablets need to reach your RustDesk server, links to the admin console, and (optionally) runs a short list of ADB diagnostics. "
       "RustDesk shows and controls the tablet screen. ADB is a separate tool that reads technical information. RustDesk does not carry ADB: the computer running ADB needs its own route to the tablet (USB, the same network, or a VPN). "
       "Nothing changes a tablet unless a super-admin turns that on in the server settings."),
    _a("rustadmin-server", "Remote support", "Setting up the RustDesk server (hbbs and hbbr)",
       "rustdesk,hbbs,hbbr,server,docker,key,ports",
       "Start the optional services with: docker compose --profile rustdesk up -d. Set RUSTDESK_HOST in .env to the public name or IP of the server. The server creates a key pair; the PUBLIC key (id_ed25519.pub) is what tablets need. "
       "Open TCP 21115-21117 and UDP 21116 on the firewall. To take upstream fixes later: docker compose --profile rustdesk pull, then up -d. Pin RUSTDESK_SERVER_TAG if you want to choose when updates land."),
    _a("rustadmin-old-client", "Remote support", "The 120 tablets run RustDesk 1.2.3 (October 2023): what to check",
       "rustdesk,1.2.3,old,client,update,security",
       "Newer RustDesk servers are expected to accept older clients, but test one tablet before changing all 120: enter the ID server, relay and key in the tablet's network settings and connect from a computer. "
       "Version 1.2.3 may lack later security fixes (check the RustDesk release notes), so plan to update the tablets when you can, and keep the server reachable only on the ports it needs. Do not assume the old client is safe because the server is new."),
    _a("rustadmin-adb", "Remote support", "Using the ADB diagnostics safely",
       "adb,debugging,commands,logcat,dumpsys,wireless",
       "ADB needs USB or wireless debugging switched on in the tablet's developer options, and the server must be allowed to run it (RUSTADMIN_ADB_ENABLED=1, adb installed in the web image with INSTALL_ADB=1). "
       "Only super-admins can run commands. Read-only diagnostics (properties, packages, permissions, settings, logs, network) run directly. Commands that change a tablet are plan-only unless RUSTADMIN_ADB_ALLOW_CHANGES=1, and then you must type the target to confirm. Every run is written to an audit log. "
       "Leaving wireless debugging open on a school network is a security risk: use a VPN and switch it off afterwards."),
    _a("rustadmin-privacy", "Remote support", "Privacy: what ADB diagnostics can expose",
       "adb,privacy,usagestats,accounts,pia",
       "Some diagnostics (app usage statistics, accounts on the device, logs, screenshots) contain personal data about the pupil or teacher using the tablet. Run them only for a support reason, do not copy the output elsewhere, and record why in your support ticket."),
]
