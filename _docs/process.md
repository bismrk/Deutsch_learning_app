- Tasks are GitHub issues in [bismrk/Deutsch_learning_app](https://github.com/bismrk/Deutsch_learning_app/issues), worked on one at a time (quick index in [`_docs/tasks.md`](./tasks.md)).
- Read the issue goal, description, and acceptance criteria (`gh issue view <number>`) before starting and before closing.
- Commit regularly in small, focused commits (referencing `#<number>` when working on an issue), and commit current work before updating documentation or starting a new task.
- Verify relevant tests (`uv run pytest` / `npm run build`) pass before committing and closing an issue.

Roles

- PM - grooms a task before anyone implements it, follows _docs/team/pm.md
- Engineer - implements one groomed task, follows _docs/team/software-engineer.md
- QA - checks the result against the acceptance criteria, follows _docs/team/qa-engineer.md

Orchestrator

The main session is the orchestrator. It launches the PM, the engineer
and QA as subagents. It does not groom, implement or test itself.

Lifecycle

1. Pick the next open issue from the backlog
2. PM grooms it
3. Engineer implements it
4. QA verifies it
5. On FAIL, back to step 3 with the QA comment as input
6. On PASS, close the issue
7. Repeat until the backlog is empty

Rules

- Do not skip step 2
- The engineer does not close the issue
- QA does not fix the code, only outputs PASS or FAIL
- The orchestrator closes the issue only after QA outputs PASS