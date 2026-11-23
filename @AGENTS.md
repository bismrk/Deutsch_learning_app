## Commands

- Backend (`backend/`):
  - `uv sync` — install dependencies
  - `uv run pytest` — run the whole backend test suite
  - `uv run pytest tests/test_health.py` — run one test file
  - `uv run alembic upgrade head` — apply PostgreSQL migrations
- Bot (`bot/`):
  - `uv sync` — install bot dependencies
  - `uv run pytest` — run bot tests
- Mini App (`miniapp/`):
  - `npm install` — install frontend dependencies
  - `npm run build` — type-check and build production bundle
- Full stack (root):
  - `docker compose up --build` — run `postgres`, `backend`, `scheduler`, `bot`, and `caddy`

## Rules

- Always communicate with the user in Ukrainian. Keep all user-facing bot/Mini App copy and LLM prompts/feedback in natural Ukrainian without calques or Russisms.
- Dependencies are added in `backend/pyproject.toml`, `bot/pyproject.toml`, or `miniapp/package.json`. Do not add a dependency without asking.
- Keep `bot/` strictly decoupled from `backend/` (communicate only over HTTP API; zero Python imports from `backend/`).
- Keep `scheduler` isolated in its own container (never run `APScheduler` inside FastAPI web workers).
- Commit current work before making doc updates or broad refactors.

## Documents

- `_docs/plan.md` — full product scope, architecture, DB schema, FSRS/LLM rules, and scoring formulas
- `_docs/process.md` — how work is organized (GitHub issues, acceptance criteria, commits)
- `_docs/tasks.md` — index of GitHub Issues (`#1`–`#19`)
- Before writing tests, read `_docs/testing-guidelines.md`
- For anything touching the UI, read `_docs/design-system.md`
