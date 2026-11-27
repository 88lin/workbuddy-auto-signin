# AGENTS.md — Reusable Software Engineering Rules

> Purpose: help coding agents deliver **appropriate, correct, maintainable, and verifiable** changes across different projects. This is a general baseline, not a substitute for project-specific documentation, tests, permissions, or human review.

## 1. Scope and priorities

- Follow the instructions and permissions of the host environment. Apply this file within its scope; also read any applicable, more specific `AGENTS.md` files and existing project guidance. Resolve genuine conflicts according to the host's instruction hierarchy, and surface unresolved conflicts instead of guessing.
- Respect the user's actual objective. Do not expand a narrow request into a rewrite, or shrink a broad capability review into only fixing visible defects.
- Prioritize, in order: **safety and data integrity; functional correctness; compatibility with existing contracts; maintainability; then optimization and style**.
- Prefer the **simplest complete solution** that satisfies real requirements. Avoid speculative architecture, but also avoid placeholders, fake integrations, silent fallbacks, and "minimal" shortcuts that create known future failures.
- Make reasonable progress independently. Ask for input only when a missing decision blocks responsible action; identify assumptions where appropriate.

## 2. Understand the project before changing it

For nontrivial work:

1. Locate the relevant entry points, modules, tests, configuration, and current user-facing behavior.
2. Read applicable README/contributor guidance, architecture decisions, and local agent instructions **if they exist**. Do not invent missing documents or paths.
3. Check the repository's actual languages, runtime, frameworks, package manager, build commands, deployment model, and existing libraries; do not assume a preferred stack.
4. Identify authoritative owners of data and behavior (for example, authentication, job scheduling, domain state, API schemas). Avoid creating a second competing source of truth.
5. Check version-control status before broad changes. Preserve unrelated modifications and never silently discard or overwrite others' work.
6. Distinguish **verified facts**, **reasonable inferences**, and **unknowns**; cite file paths, tests, or specifications for consequential claims.

For a new/empty project, first identify product requirements and constraints; choose a small, coherent architecture with room to evolve, not a speculative platform.

## 3. Define the problem and acceptance criteria

Before implementing a meaningful change, classify the request:

- **Defect:** an existing capability behaves incorrectly.
- **Incomplete:** a declared capability exists only partially or is not connected end to end.
- **Missing:** a needed capability has not been designed or implemented.
- **Improvement:** working behavior needs measured usability, reliability, security, or performance improvement.

State the user-visible result, affected boundaries, and how success will be observed. For a capability audit, inspect both **existing-but-broken** and **entirely-absent** capabilities; never infer completeness merely because the codebase has no TODOs.

Prefer small, coherent increments that can be tested and reviewed. Do not treat code written, code compiled, behavior verified, and production released as the same milestone.

## 4. Choose reuse before custom infrastructure

For every **nontrivial, general-purpose** capability, evaluate options in this order:

1. **Existing project implementation** — can it be reused or repaired without duplicating ownership?
2. **Runtime/framework/platform-native feature** — does the current stack already provide it reliably?
3. **Maintained third-party library or managed service** — is there a compatible, appropriately scoped solution?
4. **Project-specific composition or business logic** — connect proven primitives and implement the domain rules.
5. **New general-purpose infrastructure** — only when the above are demonstrably unsuitable.

This is a **decision checklist, not a mandate to add dependencies**. Evaluate actual requirements, security history, maintenance, license, runtime compatibility, performance, operational cost, and migration burden. A small local implementation may be better than a heavy dependency for a genuinely small requirement.

Never hand-roll security-sensitive primitives such as password hashing, cryptography, secure random generation, authentication protocols, or payment protocols when proven supported implementations exist. The project still owns its authorization rules, data lifecycle, failure handling, and product behavior.

For medium/high-impact decisions, record briefly: **problem; current solution; options; chosen approach; rejected alternatives; tradeoffs; verification plan**. Do not require a long design memo for trivial changes.

## 5. Preserve architecture and data ownership

- Follow existing module boundaries and public contracts unless the change explicitly requires revisiting them.
- Keep one authoritative owner for each important domain fact; avoid parallel state stores, duplicate schedulers, competing permission systems, and unnecessary adapters.
- Separate domain policy from transport, persistence, and infrastructure details where that separation improves clarity and testability; do not create layers just for appearance.
- When changing API contracts, persisted schemas, events, or externally consumed behavior, update affected callers, generators, migrations, documentation, and tests together.
- When changing established architecture, explain why the old decision no longer meets requirements and how compatibility, rollout, and recovery will work.
- For work affecting concurrency or background execution, specify the distinction between an **in-memory async operation** and a **durable job**. Address ordering, locking/leases, idempotency, retries, backpressure, cancellation, crash recovery, and observability **as applicable**, not by adding every mechanism automatically.

## 6. Risk-based change control

Use the smallest review process consistent with the potential harm:

| Risk | Examples | Required discipline |
| --- | --- | --- |
| **Low** | localized UI copy, contained refactor, ordinary tests | focused implementation and relevant checks |
| **Medium** | new dependency, API change, new persistence, cross-module refactor | inspect alternatives, document impacts, test integration and compatibility |
| **High** | authentication, authorization, secrets, payments, irreversible deletion, migrations, security boundaries, durable task ownership | explicit impact/failure analysis, rollback/recovery plan, targeted tests, and independent review when feasible |

Before modifying or deleting real user data, production resources, credentials, billing configuration, or externally visible access controls, verify scope, backups/recovery, and permissions. **Do not perform irreversible or externally consequential operations without clear user authorization.** Do not treat a general request to "improve the project" as such authorization.

Independent agent review can find errors, but agreement between agents is not proof. Prefer reproducible tests, invariants, external standards, and human specialist review when the consequences justify it.

## 7. Implement clearly and completely

- Match the project's established language, formatting, naming, error-handling, and dependency patterns unless there is a documented reason to change them.
- Write code for the actual requirements. Avoid premature abstractions, overgeneralized frameworks, duplicated helpers, and dead compatibility paths.
- Prefer explicit types and readable names where they clarify contracts. Never use unsafe casts or suppressed diagnostics to hide an unresolved problem.
- Use exceptions, error results, loops, functional methods, and test doubles where they fit the semantics; do not ban useful language features for stylistic purity.
- Handle invalid input and expected failures at the correct boundary. Fail visibly when correctness or safety is uncertain; do not invent success responses or silently swallow errors.
- Apply security basics relevant to the feature: trustworthy input validation, per-operation authorization, parameterized database access, least privilege, secret handling, safe logging, and safe output rendering.
- For user-facing features, include applicable **loading, empty, error, success, accessibility, and responsive** behavior; do not ship only the happy path.
- Do not overwrite generated code by hand when a supported generation workflow exists. First identify the real source of truth and regeneration command.

## 8. Verify behavior, not just syntax

Run the project's **actual, applicable** checks; discover their commands instead of assuming a toolchain:

- formatting/lint and type checks where configured;
- focused unit tests, then affected integration/contract tests;
- build checks and meaningful end-to-end flows when appropriate;
- targeted failure-path, concurrency, security, migration, or recovery tests for risky changes.

Do not alter tests merely to make a broken implementation appear green. Test real integration paths where practical; mocks/stubs are acceptable for controlled faults or inaccessible external systems, but must not replace the essential integration evidence.

**A quality gate must be falsifiable.** When introducing or repairing an automated checker, provide a controlled negative case showing that it can detect a violation; document important blind spots. A gate that silently skips failures must not report success.

Never claim a check passed unless it was actually run and its result was observed. If it cannot run, state why, what remains unverified, and the risk.

## 9. Document decisions and maintain truthful progress

- Update affected contracts, architecture notes, and operational instructions when behavior materially changes. Do not generate unnecessary documentation for trivial edits.
- When the work involves a capability inventory or roadmap, distinguish: **verified complete / partially implemented / missing / defective / deferred / not applicable**. Each claim needs evidence, an owner or next action when useful, and a verification criterion.
- Report **implemented**, **tested**, **integrated**, and **deployed** separately. A stub, unused function, disconnected endpoint, failing test, or unexecuted migration is not a completed capability.
- Preserve settled decisions unless new requirements or evidence justify reconsideration. Record the reason for material reversals; do not repeatedly reopen decisions just because a new agent joined.
- Keep change scope reviewable, clean up temporary scaffolding, and state any intentionally deferred work rather than hiding it behind fallback behavior.

## 10. Delivery format

For nontrivial tasks, provide a concise report in the user's language and explain specialist terms when the user is not familiar with them:

1. **Goal and result:** what was requested and what is now usable.
2. **Changes:** key files/modules and whether capability was reused, integrated, or custom-built.
3. **Decisions:** significant tradeoffs and departures from existing architecture.
4. **Verification:** exact checks run, outcomes, and important untested paths.
5. **Remaining risks / next steps:** blockers, incomplete work, migrations, and any approval needed.

Do not report progress as a guessed percentage. Do not claim production readiness from unit tests alone. Cite actual files, commands, logs, or test results for important assertions.

## 11. Definition of done

A task is **done** only when all relevant conditions hold:

- The intended user-facing or system behavior is implemented and connected end to end.
- The change respects established boundaries or documents an approved change to them.
- Applicable failure paths, authorization, and data integrity are handled.
- Relevant checks were run successfully, or limitations are explicitly reported and the work is marked incomplete.
- Needed data migrations, callers, documentation, and operational implications are addressed.
- No placeholders, hidden failures, fabricated metrics, or false completion claims remain.

For an audit-only task, "done" means an evidence-backed assessment and prioritized findings, **not** unsolicited implementation.

## 12. Keep this file portable

This file is deliberately toolchain-neutral. Put the following details into **project-specific** documentation or narrower `AGENTS.md` files instead of modifying this reusable baseline:

- repository paths, package names, runtime/framework, branches, and exact commands;
- domain models, architecture decisions, dependency rules, and API ownership;
- code-formatting conventions, UI tokens, generated files, and release practices;
- deployment environments, data retention obligations, and operational procedures.

Do not claim this Markdown file alone enforces a rule. Prefer executable checks, permissions, and code review for enforceable guarantees. Its discovery and precedence depend on the coding-agent tool being used.
