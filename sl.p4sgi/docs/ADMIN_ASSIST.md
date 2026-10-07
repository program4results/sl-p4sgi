# Admin assist and Help database (0.4.22, experimental)

For administrators who are not security specialists. In **Security audit**, tick rows (or *Select all* / *Select none*) and press
**Admin assist (N)**, or press **Assist** at the end of one row. A pop-up chat explains the issue in plain words and offers next
steps as buttons ("Do you want to remove this app's access?", "Do you want an email drafted to the people using it?"). You can also
type your own question. **Help & guides** (own panel) holds searchable guides and an "Ask" box.

## What it can and cannot do
| Step offered | What happens | Who |
|---|---|---|
| Explain / follow-up question | AI answers in plain language | anyone in scope |
| Draft an email | Text plus a `mailto:` link that opens **your** mail program. The dashboard sends nothing | anyone in scope |
| Prepare steps (revoke app access, suspend an account, wipe a device) | Dry-run GAM commands to copy. **Nothing is run.** Super-admin accounts are refused | super-admin |
| Approve an app as known | Needs a written reason and a confirm click; changes only this dashboard's label (see SECURITY_AUDIT.md) | super-admin |
| Save advice to the help database | Saved as a *draft* | anyone |

The AI only chooses from this fixed list. The **server** validates every option: unknown actions, non-super-admin dangerous actions and
targets that are not among the rows you selected are dropped. Rows are re-read server-side with your domain scope; the browser sends keys only.

## Privacy design
* The model receives **labels** (U1 users, A1 apps, D1 devices), risk flags, permission names, counts, dates, device model/OS.
  **Never** email addresses, names or serial numbers. Anything you type is scrubbed of email addresses before it is sent.
  Real values are put back only in the reply shown to you.
* Default engine is the **local Ollama** model: nothing leaves the server.
* **Gemini** is used only if you set `CHAT_CLOUD_ENABLED=1`, `GEMINI_API_KEY`, `GEMINI_MODEL` in `.env` (and `docker compose up -d`).
  Use a **paid / Vertex-grade key**: free consumer keys may use prompts to improve Google products. `ASSIST_ENGINE=local` forces local.
  If Gemini fails the assistant falls back to the local model, and then to a built-in explanation if no model answers.
  Note `CHAT_CLOUD_ENABLED=1` also makes any other provider key you set available to the Ask-the-data panel.
* Prompt injection: app names and other fields come from outside the organisation. They are length-limited and stripped of control
  characters, and cannot trigger anything because the AI can only propose buttons that the server validates and you must click.

## Help database that keeps growing
* 16+ built-in plain-language guides (`help_seed.py`, read-only, in code).
* Every first answer of an Assist session and every question in Help is saved **generalised** ("the user", "the app"; no emails) as a
  **draft** in `DATA_DIR/help/articles.json`; repeated questions are de-duplicated and counted ("asked N×").
* Only a **super-admin** can approve, archive, delete or write guides. Approved guides are searchable by everyone, are fed back to the
  assistant as context for Help questions, and are added to the Ask-the-data knowledge index when approved or on *Reindex knowledge*.
* Helpful / Not helpful votes rank guides.

## Files, limits, rollback
* New: `app/admin_assist.py`, `app/help_seed.py`, `tests/test_admin_assist.py`. Endpoints `/api/v1/assist/*`, `/api/v1/help/*`,
  `/api/v1/security/approved-apps`. Own JSON state (`DATA_DIR/help`, `assist/audit.jsonl`, `security/approved_apps.json`); no DB change.
* Limits: 25 rows per session, 20 users per app, 12 turns, sessions kept in memory for 1 hour (lost on restart), 200 sessions.
* Audit log `DATA_DIR/assist/audit.jsonl` records who did what (no free-text content, no model replies).
* Rollback: set `ASSIST_ENABLED=0` (the buttons then show an error and everything else is unchanged) or `git checkout` the previous branch and rebuild.

## Not verified
Tested with the model mocked (152 tests, headless-browser walk-through). **Not** verified here: a real Gemini call, the quality/format of
real Ollama answers (bad JSON falls back to plain text plus default buttons), and the Docker image build. Treat AI wording as a draft;
the GAM syntax in plans comes from GAM7 docs and has not been run against your tenant: test on one account first.
