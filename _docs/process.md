- Tasks are GitHub issues in [bismrk/Deutsch_learning_app](https://github.com/bismrk/Deutsch_learning_app/issues), worked on one at a time (quick index in [`_docs/tasks.md`](./tasks.md)).
- Every task must be groomed using [`_docs/task-template.md`](./task-template.md) before implementation begins.
- Read the acceptance criteria (`gh issue view <number> -R bismrk/Deutsch_learning_app`) before starting implementation, during QA verification, and before closing.
- Commit regularly in small, focused commits (referencing `#<number>`), and commit current work before updating documentation or starting a new task.

## Roles

- **PM** — grooms a task before anyone implements it; follows [`_docs/team/pm.md`](./team/pm.md) and [`_docs/task-template.md`](./task-template.md).
- **Engineer** — implements one groomed task and writes/runs tests; follows [`_docs/team/software-engineer.md`](./team/software-engineer.md).
- **QA** — checks the finished work against the acceptance criteria and runs tests; follows [`_docs/team/qa-engineer.md`](./team/qa-engineer.md).

## Orchestrator

The main session is the orchestrator. It launches the PM, the Engineer, and QA as subagents. It does not groom, implement, or test itself.

## Lifecycle

1. Pick the next open issue from the backlog (`gh issue list -R bismrk/Deutsch_learning_app`).
2. PM grooms it in place using `_docs/task-template.md`.
3. Engineer implements it, runs the full test suite, commits the work, and comments on the issue.
4. QA verifies every acceptance criterion and runs the test suite, posting a `PASS` or `FAIL` comment on the issue.
5. On `FAIL`, go back to step 3 with the QA comment as input.
6. On `PASS`, the orchestrator closes the issue (`gh issue close <number> -R bismrk/Deutsch_learning_app`).
7. Repeat until the backlog is empty.

## Rules

- Do not skip step 2.
- The Engineer does not close the issue.
- QA does not fix the code, only outputs `PASS` or `FAIL`.
- The orchestrator closes the issue only after QA outputs `PASS`.
