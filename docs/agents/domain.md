# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root.
- **`docs/adr/`** — read ADRs that touch the area you're about to work in.

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront.

## File structure

Single-context repo:

```
/
├── CONTEXT.md
├── docs/adr/
└── src/
```

## Use the glossary's vocabulary

When naming a domain concept in an issue title, refactor proposal, hypothesis, or test name, use the terms defined in `CONTEXT.md`. Avoid terminology that the glossary explicitly avoids.

If a needed concept is absent, either reconsider the terminology or note the genuine gap for `/domain-modeling`.

## Flag ADR conflicts

If an output contradicts an existing ADR, state that explicitly rather than silently overriding it.
