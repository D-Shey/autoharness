<!-- >>> autoharness:directives -->
# ZCode AutoHarness Autonomous Directives

<EXTREMELY_IMPORTANT>
## CRITICAL MANDATORY BEHAVIOR: AUTOHARNESS AUTONOMOUS EXECUTION
You are paired with the AutoHarness engineering workflow for autonomous agentic software development.
You MUST ALWAYS autonomously use AutoHarness on Turn 1 of any non-trivial user task (features, bugfixes, refactoring, systems).
Do NOT wait for the user to say "Use autoharness". Run it immediately:
`autoharness --agent zcode init "<user's task verbatim>" --mode full`

### Strict Software Engineering & Craftsmanship Invariants:
1. **Architectural Decomposition (--components)**:
   Every non-trivial software engineering deliverable MUST be decomposed into 2-4 decoupled, independently testable architectural components / modules / layers in `autoharness brief`:
   `--components "<comp1>: <responsibility & interface> - verify <test/assertion>; <comp2>: ..."`
   (e.g., C1: domain models & persistence schema; C2: core business logic / service layer; C3: API endpoints / CLI commands; C4: integration test suite).
   NEVER build a monolithic deliverable in a single pass. You MUST build, test, and register each component in isolation
   (`autoharness evidence add <file> --kind component --for C1`) before full integration.

2. **Craftsmanship Contracts (--techniques)**:
   You MUST declare explicit professional engineering craftsmanship standards and invariants in `autoharness brief`:
   `--techniques "<tech1>: <principle & enforcement>; <tech2>: ..."`
   (e.g., clean-architecture: domain core has zero external framework dependencies; strict-type-contracts: 100% typed with mypy/tsc strict; boundary-schema-validation: all inputs validated via schemas; zero-mock-integration-tests: tests verify against real state or in-memory fixtures; idempotent-mutations: mutating operations safe to retry; structured-errors: typed domain exceptions).

3. **Structural Stencils & Invariants (stencil scan)**:
   Use `autoharness stencil scan` to extract structural invariants whenever reference code, API schemas, AST structures, or specifications exist:
   - Python/TypeScript AST skeletons: `autoharness stencil scan <file.py> --mode ast`
   - JSON Schemas / API contracts: `autoharness stencil scan <spec.json> --mode schema`
   - Visual / UI wireframes (if media): `autoharness stencil scan <ref.png> --mode contour`
   Verify structural conformance with `autoharness stencil check <stencil-id> <target>`.

4. **Rigorous Verification & Test-Driven Discipline**:
   Every check must execute real assertions with pass/fail exit codes (`autoharness verify run "<test command>"`). Hollow checks or echo commands without assertions are rejected.

5. **Independent Critic Review**:
   Before declaring any task done, request independent review via `autoharness critic`. Resolve all findings before `autoharness teardown`.
</EXTREMELY_IMPORTANT>
<!-- <<< autoharness:directives -->
