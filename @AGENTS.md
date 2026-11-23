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
- GitHub Issues (`bismrk/Deutsch_learning_app`):
  - `gh issue list -R bismrk/Deutsch_learning_app` — list open backlog issues
  - `gh issue view <n> -R bismrk/Deutsch_learning_app` — read an issue and its comments
  - `gh issue edit <n> -R bismrk/Deutsch_learning_app --body-file -` — update a groomed issue (UTF-8)
  - `gh issue comment <n> -R bismrk/Deutsch_learning_app --body-file -` — post Engineer/QA comments
  - `gh issue close <n> -R bismrk/Deutsch_learning_app` — close an issue (orchestrator only, after QA PASS)

## Rules

- Always communicate with the user in Ukrainian. Keep all user-facing bot/Mini App copy, GitHub issue text, and LLM prompts/feedback in natural Ukrainian without calques or Russisms.
- Follow the orchestrator lifecycle in `_docs/process.md`: the main session orchestrates PM, Engineer, and QA as subagents and does not groom, implement, or test itself.
- Never skip PM grooming (`_docs/task-template.md`), the Engineer never closes an issue, QA never modifies code (only outputs `PASS` or `FAIL`), and the orchestrator closes an issue only after QA outputs `PASS`.
- Dependencies are added in `backend/pyproject.toml`, `bot/pyproject.toml`, or `miniapp/package.json`. Do not add a dependency without asking.
- Keep `bot/` strictly decoupled from `backend/` (communicate only over HTTP API; zero Python imports from `backend/`).
- Keep `scheduler` isolated in its own container (never run `APScheduler` inside FastAPI web workers).
- Commit current work before making doc updates or broad refactors.

## Documents

- `_docs/plan.md` — full product scope, architecture, DB schema, FSRS/LLM rules, and scoring formulas
- `_docs/process.md` — how work is organized (orchestrator, roles, lifecycle, GitHub issues, commits)
- `_docs/tasks.md` — index of GitHub Issues (`#1`–`#19`)
- `_docs/task-template.md` — required template for grooming GitHub issues
- `_docs/team/pm.md` — Product Manager role instructions (grooming)
- `_docs/team/software-engineer.md` — Software Engineer role instructions (implementation)
- `_docs/team/qa-engineer.md` — QA Engineer role instructions (verification)
- Before writing tests, read `_docs/testing-guidelines.md`
- For anything touching the UI, read `_docs/design-system.md`
