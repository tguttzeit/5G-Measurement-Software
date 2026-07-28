# Decision records

Short, dated write-ups of non-obvious design/architecture decisions made while implementing an
issue — the reasoning behind a choice, not just what was chosen. These exist so the reasoning is
retraceable later (e.g. for writing up the project), separate from the code itself and separate
from GitHub issues (which track *what* needs doing, not the discussion of *how* it got decided).

## When to add one

When a decision involved weighing real trade-offs and wasn't obvious from just reading the issue —
typically while working through the "Suggested approach" section of an issue with the maintainer
before or during implementation.

## Format

One file per decision: `NNNN-short-title.md`, numbered sequentially. Each file:

- **Context** — what problem/issue this decision belongs to, linked to the GitHub issue number.
- **Decision** — what was decided, stated plainly.
- **Reasoning** — why, including alternatives that were considered and rejected, and why.

Cross-link from the GitHub issue (a comment linking to the file) once the decision is made.
