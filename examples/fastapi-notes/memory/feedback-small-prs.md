---
name: feedback-small-prs
description: Team feedback — keep changes small and focused; one endpoint or one fix per change, with its tests. Large refactor proposals get rejected in review.
metadata:
  type: feedback
---

The team reviews everything by hand: **one endpoint or one fix per change**,
shipped with its tests ([[testing-conventions]]).

**Why:** two large "cleanup + feature" PRs caused regressions in May.
**How to apply:** when a task tempts you to refactor adjacent code, note the
refactor as a follow-up instead of bundling it.
