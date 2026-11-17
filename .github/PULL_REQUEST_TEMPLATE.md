<!--
Thanks for the pull request. Before opening it:
  1. Title follows Conventional Commits: type(scope): summary,
     e.g. "feat(core): give dominant values their own predicate".
     Breaking change: add "!" after the type or scope, e.g. "feat(core)!: ...".
  2. Run `make check` locally. CI runs the same targets.
  3. Keep this description up to date as the PR changes.
  4. Keep every heading. Write "No" or "N/A" rather than deleting one.
-->

Closes #

### What changes were proposed in this pull request?

<!--
What changed and how it resolves the linked issue.
For refactors, show the structure before and after.
Link any design note or ADR in docs/adr/.
-->

### Why are the changes needed?

<!--
The problem or use case. For a bug, explain why the old behaviour is wrong.
-->

### Does this PR introduce _any_ user-facing change?

<!--
Anything a user can notice: API, CLI flags, output format, defaults, error
messages, supported versions. If yes, show the previous and the new behaviour,
ideally with example output. If no, write "No".
-->

### How was this patch tested?

<!--
Tests added or changed, with positive and negative cases.
Commands a reviewer can copy and paste.
For performance changes, before and after numbers from bench/.
If no tests were added, explain why.
-->

### Does this PR potentially affect any of the following?

<!-- Answer yes or no. Every "yes" needs a sentence in the sections above. -->

- Public API of the Python package or the Rust crate:
- Predicate output or skew report format:
- Supported versions (Python, Spark, databases, Rust MSRV):
- Dependencies (added, removed or upgraded):
- CI, release or publishing workflows:
- Performance of planning or of the generated queries:

### Why is this PR large?

<!--
Required only if CI labels this PR size/XXL (more than 1500 changed lines).
Explain why it cannot be split. Otherwise write "N/A".
-->

### Checklist

- [ ] Linked issue, `type/*` label, `area/*` labels and milestone are set
- [ ] Tests cover the change; a bug fix includes a test that fails without the fix
- [ ] Docs and README updated where behaviour changed
- [ ] CI updated if this PR adds a component, folder, tool or supported version
- [ ] Breaking change: `!` in the title, `breaking-change` label, migration note above
