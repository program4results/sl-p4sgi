# Ask the data (AI chat) + pgvector — 0.4.21, EXPERIMENTAL

A collapsed **Ask the data (AI)** panel. Local **Ollama** by default. Two question types:
* **Database** (super-admin): the model writes ONE read-only SELECT over curated `chat_*` views; the SQL is shown with the result.
* **PIA / docs** (any signed-in user): similarity search over the PIA workbook, schema notes and docs, answered with numbered sources.

Code: `apps/api/app/ask_data.py` · Tests: `apps/api/tests/test_ask_data.py` · DB image: `deploy/db/Dockerfile`

## Safety model
* Raw tables are never exposed. Views: `chat_schools, chat_devices, chat_telemetry, chat_attendance, chat_publish_events, chat_jobs`.
  They omit learner names (`absent_named` is reduced to a count), serials, tablet IDs, SIM/WhatsApp numbers, school emails and telemetry payloads.
* Model SQL is validated (single SELECT, view allow-list, no comments/DDL/DML/system functions/quoted identifiers/comma joins), then run in a
  READ ONLY transaction with `statement_timeout` (8 s) and a row cap (200). One automatic repair attempt, then it stops.
* **Cloud models (Grok, ChatGPT, Claude, Gemini) are OFF.** To enable later: `CHAT_CLOUD_ENABLED=1` plus `<PROVIDER>_API_KEY` and `<PROVIDER>_MODEL`
  in `.env` (`XAI_`, `OPENAI_`, `ANTHROPIC_`, `GEMINI_`). A cloud model sees only the question, the view schema and (docs mode) retrieved PIA text. Result rows
  never leave the server; the answer summary is always written by local Ollama. These adapters are unit-tested with mocks only, **not against the live APIs**.
* Every question is appended to `docker-data/sl.p4sgi/chat/audit.jsonl` (who, question, SQL, provider, row count). Result rows are not logged.
* Additive: one guarded block in `main.py`; kill switch `ASK_DATA_ENABLED=0`. The module only creates new objects (`chat_*` views, `chat_chunks` table, `vector` extension).

## pgvector
`deploy/db/Dockerfile` builds **the same `postgres:16-alpine`** plus pgvector v0.8.0 and `docker-compose.yml` uses it for `db`.
Do NOT switch to the Debian-based `pgvector/pgvector:pg16` image on the existing data directory: glibc vs musl collation differences can silently corrupt
text indexes (`schools.emis`, `attendance_sync_events.session_id`).
Embeddings: `nomic-embed-text` via Ollama, 768 dimensions, stored with the model name per chunk, so they can be rebuilt with another model.
If the extension is missing the chat still works with full-text search and the status line says so.
Vertex / Google Cloud later: Cloud SQL and AlloyDB for PostgreSQL support pgvector, so this schema and the `<=>` queries move as is. Use an embedding model
configured to 768 dimensions (or change `EMBED_DIM` and Reindex) and keep result rows local as above.

## Deploy (first time needs a db restart)
```bash
cd /home/george/drive_14tb/stacks/sl.p4sgi
# 0. backup first
docker exec sl-p4sgi-db pg_dump -U slp4sgi slp4sgi > ~/slp4sgi-before-pgvector.sql
# 1. let containers reach Ollama on the host
sudo systemctl edit ollama      # add:  [Service]  Environment="OLLAMA_HOST=0.0.0.0"
sudo systemctl restart ollama && ollama pull nomic-embed-text
# 2. rebuild (rebuilds the db image with pgvector; data directory is reused)
git fetch origin && git checkout feature/ask-data-chat
docker compose up -d --build
# 3. check
curl -s http://127.0.0.1:8088/api/v1/chat/status | python3 -m json.tool | head -30
```
Dashboard: hard-reload → *Ask the data (AI)* → **Check status** → (super-admin) **Set up database objects** → **Reindex knowledge** → ask.
Binding Ollama to 0.0.0.0 exposes it on your LAN: firewall port 11434 or restrict to the docker bridge.

Rollback: `ASK_DATA_ENABLED=0`; to drop the objects: `DROP VIEW chat_schools, chat_devices, chat_telemetry, chat_attendance, chat_publish_events, chat_jobs; DROP TABLE chat_chunks;`.

## NOT done / limits
* The pgvector image build, Ollama connectivity from the container and real model output quality are **unverified** here (no Docker, no Ollama in the sandbox); tests mock them.
* A 14B local model will sometimes write wrong SQL: always read the SQL shown. Try `qwen3:14b` or `gpt-oss:20b` from the model list.
* GAM report data (users, logins, devices from Workspace) and the Security / Privacy panels' results are not queryable yet (next phase: aggregate-only tools).
* SQL mode is super-admin only: no per-domain scoping is implemented for the views.
