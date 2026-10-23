# ADR-005: Delegating the unittest lifecycle to the standard library

- Status: Accepted
- Type: architecture decision record (ADR)
- Applies to: 0.5.0 and later
- Related: [invariant registry](../INVARIANTS.md), [ADR-004](ADR-004-process-containment.md)

## Context

The runner supports pytest-style test functions, unittest test cases, plain
assertions with rewritten messages, fixtures with several scopes, and phase
budgets that must interrupt a hung `setUp`, body, `tearDown` or cleanup without
killing the worker.

Reimplementing the lifecycle means executing `setUp`, the test method,
`tearDown`, the cleanup stack, skip markers and expected-failure markers inside
runner code that mirrors the standard library. That design is a compatibility
trap. `unittest` semantics are a long list of small, interacting rules, and each
rule is enforced by user code somewhere in the ecosystem:

- `doCleanups` runs in LIFO order, and it runs **even when
  `setUp` failed**.
- `expectedFailure` covers the test method, not `setUp`.
- `skip` set on a class must be visible before the method runs.
- `subTest` failures are collected per subtest and can coexist with a passing
  body.
- `IsolatedAsyncioTestCase` has its own loop, its own `asyncSetUp` and
  `asyncTearDown`, and `addAsyncCleanup`, which is dispatched by coroutine
  inspection.
- `setUpClass` failures skip every test in the class without running per-test
  bodies.

Every mirror of those rules is a place to diverge, and divergence is invisible:
it shows up as a test that passes here and fails upstream, or as a cleanup that
silently does not run. The maintenance cost grows with every Python release,
because the standard library keeps refining these paths.

## Decision

The lifecycle belongs to `unittest`. The shim runs test cases through stock
`unittest.TestCase.run`, with `IsolatedAsyncioTestCase` driven by its own
runner, and contributes only the parts the standard library does not provide:

1. **A result subclass, not a lifecycle.** `ZtestResult(unittest.TestResult)`
   subscribes to the public result hooks (`addError`, `addFailure`, `addSkip`,
   `addExpectedFailure`, `addUnexpectedSuccess`, `addSubTest`, `stopTest`) and
   turns them into runner events. The primary verdict is read only from the
   public result lists (`failures`, `errors`, `expectedFailures`,
   `unexpectedSuccesses`, `skipped`). `_callTestMethod` is never touched, and
   `_Outcome` is never consulted for the verdict. Beyond that public surface
   the shim contacts a fixed, short list of private attributes, each with a
   single purpose and no role in the verdict:
   - `case._outcome` — re-created with a stock `_Outcome(result=zr)` before
     the safety-net `doCleanups()` of point 8 whenever the attribute is
     `None`, then reset. The check lives in the common `finally` around
     `run()` and fires on every exit path that leaves `_outcome` unset —
     the normal path, where `run()` itself resets it, and the
     control-exception path alike — so the cleanup never drops the rest of
     the list at the first callback failure. It is write-only: no outcome
     internals are read, and a patch release renaming the class fails loudly
     as a teardown diagnostic.
   - `case._asyncioRunner` and `runner._loop` — read to cancel pending
     runner tasks after an async timeout. Both reads are guarded; a missing
     attribute degrades to a quiet best-effort no-op, not an error.
   - `case._callTearDown()` — called once on the control-exception path when
     the body started but `tearDown` did not run.
   - `case._testMethodName` — read to wrap the test-method attribute.
   - `cls._class_cleanups` and `unittest.case._module_cleanups` — the cleanup
     stacks, popped manually with per-callback budgets on the paths where
     the standard library's own drain would drop the whole stack (a
     `BaseException`-class timeout); a missing stack falls back to the public
     `doClassCleanups()` / `doModuleCleanups()` or skips the loop.
   - `case._setupAsyncioRunner` — presence check that identifies an
     `IsolatedAsyncioTestCase`; never called.
   These contacts are the entire private surface. A Python patch release
   that renames one of them degrades that one adapter — loudly for
   `_Outcome`, quietly for the async task drain — while the verdict
   accounting, derived from the public lists, stays untouched.
2. **Capture outside the lifecycle.** Output capture is a context manager that
   wraps `run()`, not a piece of the lifecycle. Nothing inside the standard
   library needs to know that output is redirected.
3. **Per-phase budgets through public methods.** `_wrap_phase` installs an
   interceptor on the case instance attributes `setUp`, `runTest` or the test
   method, `tearDown`, `doCleanups`, `asyncSetUp` and `asyncTearDown`. The
   interceptor arms a phase budget, records timestamps, checks the sticky timer
   flag at the accounting boundary, applies kind and result guards, and restores
   the original attribute afterwards. The standard library still decides what to
   call and in which order.
4. **Markers are copied onto wrappers.** `__unittest_skip__` and
   `__unittest_expecting_failure__` are copied from the original method to the
   wrapper, because `run()` reads them from the bound method it is about to
   call. A wrapper without the markers would silently un-skip a skipped test.
5. **A never-executed sentinel.** `_ztest_body_started` is set by the body wrapper and
   is the only evidence that the test body actually ran. It is immune to xfail
   conversion, so a body skipped because `setUp` failed cannot be reported as an
   expected pass.
6. **Teardown dominance through a flag.** A timeout inside `tearDown` or
   `doCleanups` sets `teardown_timed_out`; `ZtestResult.stopTest` consumes it
   once and marks the timeout as dominant, so the item reports a timeout failure
   rather than whatever the body produced.
7. **Teardown diagnostics by phase, not by order.** `_PhaseBudgets` keeps a
   phase stack (`phase`, `phase_off`). `in_teardown_phase()` decides whether an
   escaping exception belongs to teardown. Because the wrapper pops the phase in
   `finally` before the standard library's `add*` hook runs, the exception is
   tagged inside the wrapper (`_tag_teardown_origin`), and the hooks recognize
   the tagged exception by membership in `teardown_errs`.
8. **Fixture ownership registered before anything runs.** Function-scope
   teardown is registered as a cleanup with
   `addCleanup(_resolver_teardown_unit, ...)` before the first fixture or hook
   executes. The standard library then runs that cleanup on every path,
   including a failed `setUp`. Autouse fixtures and conftest setup and teardown
   hooks run for unittest items as well. A `case.doCleanups()` call in `finally`
   around `case.run()` is the safety net for control-exception paths and for the
   early return on skip. When preparing a unittest item fails before `run()`,
   the remaining finalizers are drained manually in LIFO order, conftest
   teardown first and then function scope, because the standard library's
   cleanup layer is unreachable at that point.
9. **Per-unit budgets on cleanup registration.** `addCleanup` and
   `addAsyncCleanup` are wrapped so each callback is a budget unit. A cleanup
   that exceeds the budget raises a cleanup-specific error, which is recorded as
   an ordinary teardown error rather than a control exception, so the rest of
   the cleanup stack still runs; interrupts and exit requests still propagate.
   Coroutine callbacks keep their async identity, since a sync trampoline would
   return a coroutine that nobody awaits.
10. **xunit pairing follows a successful setup.** `setup_method` registers its
    `teardown_method` immediately after success, and `setup_function` registers
    `teardown_function` the same way, before fixture resolution. A teardown-only
    hook without a setup is legal and registers unconditionally. Conftest
    teardown hooks register right after the resolver owner. Execution order
    stays LIFO across the three layers: xunit, conftest, fixture finalizers.
11. **Class setup runs under a budget.** `setUpClass` and `setup_class` execute
    inside `budgets.phase("setup_class")` before any user code, so a hung class
    setup becomes a timeout failure instead of a hung run.
12. **Skip semantics are bridged, not reimplemented.** The shim's `Skipped`
    derives from `BaseException` so a user `except Exception` cannot swallow it,
    and the standard library would route it to the error channel. The bridge in
    `ZtestResult.addError` moves it to `addSkip` while preserving timeout
    dominance. An explicit `except (Skipped, unittest.SkipTest)` in the item
    runner handles a skip raised during preparation and a native `SkipTest` from
    `setUpClass`, and the class is skipped deterministically through the
    failed-class cache.

## Consequences

**Teardown errors cannot be hidden.** Any error from `tearDown`, `doCleanups` or
a fixture finalizer lands in `outcome.teardown_errs`. When the test body itself
fails and teardown also fails, `teardown_errs` blocks xfail conversion so an
expected body failure cannot mask an unexpected finalization failure (both are
reported, matching pytest's `FAILED` + `ERROR` pair). When the body succeeds and
a teardown exception is raised, the item becomes a failure unless a single
finalizer exception (or, when multiple finalizers fail, their resulting
`BaseExceptionGroup`) matches a declared `@pytest.mark.xfail(raises=...)`
expectation on the item. Without this rule the run would report success while a
fixture leaked a resource.

**xfail stays safe.** Conversion to an expected failure requires the body to
have entered (`_ztest_body_started`), no unexpected teardown diagnostics, and a
failure that is not a control exception. Timeouts, interrupts and the
never-executed sentinel are never converted.

**Cleanups run exactly once.** The standard library pops entries from
`_cleanups`, so a cleanup cannot run twice, and `teardown_scope` is idempotent,
so a scope that is finalized by both the cleanup stack and the exit path runs
its finalizers once. The safety-net `doCleanups()` in `finally` therefore adds
coverage without adding duplicate executions.

**Costs.** Phase wrappers add one frame per lifecycle call, which shows up in
tracebacks of failing tests and must stay out of the reported traceback body.
The wrapper installer must keep the marker copying and the captured method set
in sync with what `run()` calls; that list is short and stable. Cleanup callback
identification depends on `inspect.iscoroutinefunction`, which is the same test
the standard library uses.

**What this buys.** A Python patch release changing unittest internals cannot
silently change the runner's results: the verdict is read only from the public
result lists, and the private surface is the fixed short list enumerated in
the decision, so a rename degrades one narrow adapter — loudly for the
`_Outcome` safety net, quietly for the async task drain — without touching
verdict accounting. The lifecycle behavior users depend on is
the behavior of the standard library they already have installed, tested by its
own suite, documented in its own docs. The shim owns only the parts that are
genuinely runner-specific: budgets, capture, event translation and the
diagnostics that the standard library does not model.
