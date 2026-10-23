# Invariant registry

ztest is a test runner: a Zig supervisor owns Python worker processes, and every
worker reports through a small wire protocol. Most of the work in this codebase
protects properties that fail silently when they break. A stale process group
gets signaled, a frame is torn in the middle, a test is reported as passed when
its body never ran.

An invariant here is one such property. Each one has a stable identifier, for
example `FINALIZER-PER-UNIT-BUDGET`.
Where a guard is non-obvious, the code tags it with a comment ending in
`Invariants: <KEY>` (one tag may name several keys):

```python
# Every finalizer runs under its own budget. Invariants: FINALIZER-PER-UNIT-BUDGET.
```

The shim carries 76 such tag lines (one line may name several keys); the
registry below holds 122 entries, and not every entry needs a code citation.
The Zig supervisor's containment guards cite ADR-004 by layer instead.
`scripts/check_docs.py` fails the build if a key cited from the code is
missing from this file.

Identifiers are permanent keys, not ordering. They are never renumbered and
never reused. An invariant that stops holding is either restored or removed
together with its guard; when the property survives in a different shape, the
entry is amended and keeps its key.

Entries are grouped by the mechanism they protect. The two architecture decision
records (ADRs) behind the process mechanics are
[ADR-004](adr/ADR-004-process-containment.md)
and [ADR-005](adr/ADR-005-unittest-delegation.md); their invariants are keyed
`ADR-004-*` and `ADR-005-*` and form the last two sections. The records start
at 004: numbers are permanent keys and are never renumbered, and
[docs/adr/README.md](adr/README.md) says why.

## Transport and process-group identity

- `TRANSPORT-AUTH` — Accept is authenticated and every frame is verified: the peer is
  identified through socket credentials (`SO_PEERCRED`), the header carries a
  CRC32 over the first 16 header bytes plus the
  payload, and all channels are
  served by one multiplexed poll loop. The command line is
  parsed strictly, so unknown
  combinations are rejected rather than ignored.
- `ZYGOTE-GROUP-IDENTITY` — Process-group identity is checked at accept:
  `getpgid(zygote_pid) == zygote_pid` through the peer credentials, otherwise
  the connection is refused. The zygote becomes a group leader before it
  connects, so a mismatch means the peer is not the process the supervisor
  believes it is talking to.
- `SIGPIPE-IGNORED` — The supervisor ignores `SIGPIPE`, and `SIG_IGN` survives `execve`.
  Both worker paths keep that state, so Python semantics hold inside the
  worker: a test writing into a dead pipe sees `BrokenPipeError` instead of
  dying by signal 13. Child processes spawned by tests get the default
  disposition back through `subprocess`'s `restore_signals=True`, so shell
  pipelines inside those children see the write contract they expect.
- `FRAME-BOUNDED` — The frame state machine is non-blocking, the per-test inactivity
  deadline is explicit, writes to the control channel are bounded, and
  aggregation fails closed.
- `OUTPUT-CHANNEL-BOUNDED` — The output channel is bounded and non-blocking, so a stalled reader
  can never block a supervisor write.
- `POLL-ITERATION-BOUNDED` — A poll iteration does bounded work. One pathological test cannot
  monopolize the scheduler or delay signal handling for the rest of the run.

## Child lifecycle

- `FRAME-RATE-LIMIT` — Outcome and collection frames are unconditional; rate-limiting is
  by event type, not by payload. The four service types (`TEST_START`,
  `PHASE_START`, `TEARDOWN_START`, `TEST_END`) act as pulses and may be
  swallowed even though they carry diagnostic fields (`nodeid`, `file`,
  `phase`): the supervisor uses them to re-arm the inactivity window, never to
  change counters. The window
  is re-armed by every successful write, never by the absence of output: a
  pulse is swallowed only when the last write landed less than 0.1 s ago, and
  a forced pulse (`TIMER-CHECKS-INSIDE-TRY`) always reaches the wire. A worker that is alive
  and busy therefore cannot be mistaken for one that is hung.
- `ZOMBIE-PINNING` — Zombie pinning is deliberate and lazy. The worker is marked
  with `WNOWAIT`, its group is swept, and `waitpid` is deferred: while the
  registry holds the process, its `pid` and `pgid` cannot be reused. The REAP
  handshake collects the real exit status later.
- `ZYGOTE-REAP-BOUNDED` — Reaping inside the zygote is bounded: a 2.5 s budget, then `SIGKILL`
  to the leader and its group, then a 1 s final reap, and `-1` when the status
  cannot be collected. The zygote command loop never blocks on a stuck worker.
- `ACCEPT-DRAIN-LOOP` — Accept and drain share one multiplexing loop; output is non-blocking
  and bounded; escaped descendants are swept; the adoption reaper reaps
  unregistered adoptees without recording their status, because a bare pid
  the kernel may already have recycled must never fabricate an outcome.
- `WORKER-PDEATHSIG-REARM` — A worker does not outlive the zygote: after fork the worker re-arms
  `PR_SET_PDEATHSIG` onto the zygote (`ADR-004-1`), so the zygote's death makes
  the kernel kill in-flight workers too. Containment of a worker is still never
  delegated to a watchdog living inside the worker process (see
  [ADR-004](adr/ADR-004-process-containment.md)).
- `SESSION-HOOKS-FAIL-CLOSED` — Session hooks fail closed; a terminal slot is killed immediately;
  accept re-arms and drains; adopted children that were never registered get no
  status recorded; orphan sweeps are unconditional.
- `HEARTBEAT-PULSES` — Liveness heartbeats are emitted at `TEST_START`, `PHASE_START`,
  `TEARDOWN_START` and `TEST_END`, subject to the rate limit of `FRAME-RATE-LIMIT`; on a
  clean PASS `TEST_END` is not sent: the summary comes from `FILE_DONE`, and
  `TEST_PASS` is queued deferred, so it re-arms the inactivity deadline only
  when it is actually written — no later than the next unconditional frame or
  flush barrier. A long phase is still distinguishable from a lost worker:
  forced pulses before a drain and the flush barriers always reach the
  supervisor.
- `ZYGOTE-SWEEP-EXCLUDED` — The zygote is excluded from orphan sweeps from before the root
  conftest preload until it is retired.
- `NO-PID-BOOKKEEPING` — Containment does not rely on pid-keyed orphan bookkeeping. A pgid
  recorded for a process that has exited may belong to an unrelated process by
  the time it is signaled.
- `SWEEP-GROUP-AWARE` — The orphan sweep is process-group aware: the run watchdog and
  protected workers are spared because they live in their own groups,
  `setsid` escapees are killed, and the zygote pid is protected in every sweep.

## Output sinks

- `SINK-MATRIX` — One matrix decides how a sink is written: a plain file uses a
  shared offset and interleaved writes stay whole; a fifo or tty gets a private
  non-blocking OFD (open file description); a socket uses `MSG_DONTWAIT`. No
  path blocks, and no path
  modifies the flags of the file it was handed.
- `FIFO-DROP-DEGRADE` — A fifo that cannot be written degrades to dropping output. The
  write still never blocks.
- `COLLECTION-DONE-GATE` — Collection closes at an explicit `COLLECTION_DONE` gate before
  `setup_module` runs. After that gate the ordinary per-test budget applies.
- `FILE-START-BUDGET` — `FILE_START` carries its own bounded budget, so importing a
  heavy conftest cannot trip the per-test inactivity window.
- `HOOK-EXPORT-FAIL-CLOSED` — Only conftest hooks that are genuinely invoked are exported to user
  code. Everything else fails closed with `ZtestUnsupportedApiError` instead of
  being silently skipped, with one deliberate exception: the recognized no-op
  reporting metadata hooks are accepted and never called (`OSS-RH`, OSS reporting hooks).
- `HOOKIMPL-FLAGS-INERT` — `pytest.hookimpl` wrapper and ordering flags are accepted so a
  conftest that uses them still loads, but they do not change execution: hooks
  are plain callables, ordering flags are ignored, and a wrapper-marked
  generator hook is driven through its pre-yield and post-yield halves back to
  back. A generator, coroutine or awaitable hook without the wrapper marking
  fails closed when the shim would invoke it; the recognized no-op reporting
  hooks are never invoked, and only their generator and async-generator forms
  are rejected (`OSS-RH`, OSS reporting hooks).
- `FILE-DONE-EXIT-DEADLINE` — A `FILE_DONE` is followed by an exit deadline, unittest class
  cleanups are accounted for, and the per-test timer is disarmed before
  teardown.

## Fork protocol

- `FORK-JSON-FRAMING` — A fork request is a single compact JSON object
  `{slot, file, opts}`. A path containing a tab or a newline cannot
  desynchronize framing. Any parse error produces an ERR line and the zygote
  keeps serving; the supervisor's fallback is to kill the zygote and retry the
  exec.
- `ZYGOTE-PRELOAD` — Root conftest files are preloaded into the zygote without
  killing it. Only `KeyboardInterrupt` propagates out of a preload. Every
  preload uses a unique module name, and a failed preload evicts the module from
  `sys.modules` so a half-initialized module never leaks into forks.
- `ARM-ORDER` — The arm order depends on the spawn path, with `SIGKILL` as
  the signal in both. A zygote-forked worker runs `setpgid` first and re-arms
  `pdeathsig` second, inside the child branch: the process group must exist
  before the parent-death contract is armed, and the kernel, not the
  interpreter, must be the one to act on it. A clean-spawn worker is armed
  pre-`execve` in the Zig child branch and never re-arms: `execve` preserves
  the arm, and `setpgid` runs later — supervisor-side right after the exec
  handshake, and again as the first statement of the Python entry point.
- `FRAME-SIGNAL-MASK` — While a frame is being written, `SIGALRM` can land only during a
  backpressure wait for `POLLOUT`: the mask is acquired before the write loop,
  held across the loop of partial writes, and lifted for the `POLLOUT` wait
  itself (the deadline is absolute), so no interrupt can split the syscall
  sequence of an unstalled frame — a split frame breaks the CRC at the
  supervisor and is indistinguishable from a worker crash. The original mask
  is restored when the flush exits. During the wait the signal can fire; at
  that point the wire may already carry a partial frame, though this is not
  guaranteed (the very first write may have stalled on `EAGAIN` with nothing
  sent). A signal or the deadline that fires during the wait ends the channel
  as terminal: further sends are skipped, and the receiver fails closed on the
  incomplete protocol — a CRC mismatch of a partial frame, or the missing
  `FILE_DONE` when no frame bytes were sent.

## Parameterization identity

- `INDIRECT-CLOSURE-AXES` — Indirect parametrization carries the full transitive closure of
  fixture axes as `(decl_token, axis_index, value)`.
- `AXIS-MEMBERSHIP-ENFORCED` — Invoking a parametrized fixture outside its axis fails collection.
  There is no silent fallback to `params[0]`.
- `FIXTURE-KEY-UNIQUE` — Fixture cache keys are collision-free across declarations, layers
  and axis indexes.
- `DECL-TOKEN-KEY` — One declaration, one token. The cache key is
  `(name, layer, decl_token, idx)`, so two declarations that happen to share a
  fixture name never share a cache record.
- `KEY-DEPENDENCY-SIGNATURE` — The key also carries the dependency signature of parametrized
  dependencies in the transitive closure, so a derived fixture is never handed
  an instance built from a foreign parameter.

## Control outcomes and discovery accounting

- `SESSION-FINALIZATION` — Session finalization runs on every path and is `BaseException` safe;
  a subtest failure takes precedence over a later pass; native
  `unittest.expectedFailure` is strict (an unexpected pass fails the item),
  while pytest-style `xfail` follows its `strict` argument with the file's
  `xfail_strict` setting as the default; timeout and exit are never converted
  to xfail.
- `TIMEOUT-IS-BASEEXCEPTION` — The timeout exception derives from `BaseException`. User code
  cannot swallow it with `except Exception`, and the xfail and skip machinery
  never matches it.
- `MARK-DECORATOR-CONTRACT` — A mark used as a decorator returns its target. A bare mark
  without arguments is legal and attaches itself, matching pytest 9: bare
  `xfail` means `reason=None, strict=False` (an XPASS stays a pass), bare
  `skipif` skips the item, and bare `usefixtures` is a no-op that emits a
  `PytestWarning`.
- `SUBTEST-CONTROL-PROPAGATION` — Control exceptions raised inside `subTest` blocks are propagated.
  They are not recorded as subtest failures and not swallowed.
- `INTERRUPT-ABORTS-FILE` — `KeyboardInterrupt` is never an xfail and never a test failure: it
  aborts the file.
- `DISCOVERY-SYMLINK-CYCLE-SAFE` — Discovery is symlink-cycle safe through a visited set, and the walk
  checks for interruption on every iteration.
- `EXPECTED-FAILURE-SETUP-SEMANTICS` — `expectedFailure` on `setUp` follows CPython semantics, and
  accounting stays single-outcome per test.
- `INTERRUPT-SAFE-TEARDOWN` — Interrupt-safe teardown: a `KeyboardInterrupt` inside any
  teardown helper is recorded and raises an internal flag, but the helper does
  not re-raise it. Restoring stdout and stderr, cleaning temporary files and
  emitting `FILE_DONE` all live in `finally` blocks that must complete; the main
  loop consumes the flag and stops the file as FAILED.
- `DISCOVERY-DEDUP-REALPATH` — Discovery targets are deduplicated and canonicalized with
  `realpath`, so a file reachable through a `./`-prefixed duplicate does not
  run twice.
- `EXPECTED-FAILURE-NO-SETUP-COVERAGE` — `expectedFailure` never covers `setUp`.
- `TEARDOWN-INTERRUPT-CONSUMED` — A teardown interrupt is consumed immediately after the per-test
  `finally`.
- `TEARDOWN-DOMINATES-XFAIL` — A teardown error dominates xfail classification, and a subtest
  failure survives a later skip in the same body.
- `NEVER-EXECUTED-SENTINEL` — The never-executed sentinel `_ztest_body_started` is immune to xfail. A
  body that never ran because `setUp` failed is never reported as an xfail pass.
- `SUBTEST-AGGREGATION-PHASE-AWARE` — Subtest aggregation is phase aware: records delivered before
  `stopTest` are folded by unittest, records delivered after it become teardown
  diagnostics.
- `CONTROL-EXCEPTIONS-CANONICAL` — The control exceptions form one canonical tuple. Interrupt,
  timeout and the never-executed sentinel are never replaced, never absorbed by
  xfail, and never rebranded at a re-raise site.

## Timeout serialization and temporary-file containment

- `TIMEOUT-INTEGER-MICROSECONDS` — `--timeout` travels as integer microseconds (`timeout_us`) and
  round-trips without loss; the worker reads no other timeout key.
- `TIMEOUT-MINIMUM-CLAMP` — A sub-microsecond timeout clamps to 1 µs, so an accepted timeout
  never disables the timer.
- `INACTIVITY-WINDOW-DOMINATES` — The supervisor's inactivity window dominates the phase floor:
  `max(timeout_ms, 1500) + 1500` ms, and one hour (3 600 000 ms) when no
  timeout was given, so a long legitimate file is never cut short while the
  user's per-test timer is off (pytest's default has no timeout).
- `SIGINT-MASKED-WITH-SIGALRM` — `SIGINT` is blocked together with `SIGALRM` on the frame
  write, extending `FRAME-SIGNAL-MASK` to both interrupt sources.
- `TIMER-DISARMED-BEFORE-TEARDOWN` — The per-test timer is disarmed before teardown starts.
- `TEMP-FILES-CONTAINED` — Temporary files are contained: session temp trees live under
  the supervisor's `sandbox` option, and the exit sweep removes them even when a
  worker was `SIGKILL`ed. Standalone runs without a supervisor keep using `/tmp`.
- `PIDFD-DEATH-DETECTION` — Supervisor death is detected through a pidfd. There is no
  polling fallback on a bare pid, because a reused pid would make the watchdog
  signal an unrelated process group.
- `CLASS-CLEANUPS-BUDGETED` — `doClassCleanups` runs per unit, and the timer is armed before the
  `try` that wraps the call: the sticky flag carries a timeout that fired at
  the arming boundary into the unit's accounting, so no gap opens between
  arming and the covered call.

## Phase budgets

- `FINALIZER-PER-UNIT-BUDGET` — Every scope finalizer is its own budget unit: heartbeat and
  re-arm before the call, disarm after it. A hung finalizer gets its own timeout
  record while the remaining finalizers still run, and the file does not end up
  CRASHED. The module-level hook lifecycle stays at module scope, so
  `teardown_scope` is not tied to a single `run_file`.
- `TIMEOUT-DOMINATES-FAILURE` — Every budget unit checks the timer flag, and a timeout dominates an
  ordinary failure. During the setup-phase drain, control exceptions take
  priority.
- `DRAIN-BOUNDED` — Drain is truly bounded: `asyncio.wait` returns when the budget
  expires regardless of how resistant the tasks are. Survivors are reported
  through the return value as runner poison and are never waited for.
- `TIMER-CHECKS-INSIDE-TRY` — Timer checks live inside the `try` that owns the phase, lifecycle
  boundaries arm explicitly, and a heartbeat is sent immediately before a drain
  so the supervisor re-arms a full window on `TEARDOWN_START`.
- `ASYNC-FINALIZER-TIMEOUT-PROTOCOL` — After an async finalizer times out: heartbeat, bounded drain,
  then poison the runner if tasks survived. A cancel-resistant task does not
  outlive its unit and does not damage the next item or the session close.

## Assert rewriting

- `REWRITE-PRESERVES-SCOPE` — The rewrite preserves scope: no lambda thunks, since `await` and
  walrus inside them broke the module. The block replaces the assert in the
  enclosing scope, evaluates each operand exactly once in source order, and
  builds the message lazily on the failing branch only. Non-compare statements
  and comparison chains stay native.
- `ARRAY-TRUTH-FALLBACK` — Assert predicates delegate truth evaluation directly to
  `bool(res)` without implicit `.all()` reduction, preserving standard Python
  truthiness on custom containers.
- `REWRITE-TEMPORARIES-CLEARED` — The rewrite clears its temporaries on the success path.
  Otherwise the operands stay referenced by the frame and falsely fail weakref,
  refcount and `__del__` tests. On failure the `AssertionError` unwinds and the
  temporaries die with the frame.
- `ALL-FALLBACK-SHAPE-GATED` — Multi-element array comparisons (such as NumPy
  `bool(arr1 == arr2)`) propagate Python's `ValueError` ("The truth value of an
  array with more than one element is ambiguous") unchanged instead of reducing
  via `.all()`, matching pytest.
- `REWRITE-DEBUG-GATED` — The rewritten block sits under `if __debug__:`. Under `-O`
  the native assert disappears, and the rewrite must not evaluate operands or
  bind a walrus that the rest of the function never expects.
- `AST-CACHE-OPT-LEVEL` — The AST cache key includes the optimization
  level, and the level is
  passed explicitly to `compile()`. Otherwise collect-only under
  `PYTHONOPTIMIZE` caches assert-stripped code and fabricates a PASS.

## Async execution

- `GENERATOR-TESTS-REJECTED` — Generator tests are rejected.
- `SYNC-COROUTINE-FAIL-CLOSED` — Returning a coroutine from sync code fails closed: a sync method
  or finalizer that returns a coroutine, generator or awaitable has that object
  closed or canceled, and the outcome is recorded as
  `_ZtestNeverExecutedError`. It is never a phantom pass.
- `ASYNC-AWARE-CAPTURE` — Capture is async aware: `IsolatedAsyncioTestCase` runs awaited with
  its own `asyncSetUp`, `asyncTearDown` and `addAsyncCleanup`.
- `RETURN-GUARDS-COROUTINE-AWARE` — Return guards are coroutine aware, and a plain `TestCase` with an
  async test method fails closed.
- `ASYNC-RESULT-VALIDATED` — The result of an async `Runner.run` is validated, so a
  coroutine-returning body fails closed instead of reporting success.
- `SUBTEST-DRAIN-IN-SETUP` — Subtest drain runs in the setup phase; cleanup is sentinel safe and
  cancels pending tasks; a hook the shim invokes that returns an awaitable is
  rejected; collect-only exit status matches a real run.
- `ASYNC-FINALIZERS-ON-RESOLVER-RUNNER` — Async fixture finalizers run on the resolver's runner, the same
  `asyncio.Runner` that drives async fixtures.
- `CLOSE-BEST-EFFORT` — Closing an unexecuted generator, asyncgen or coroutine result is
  best effort, and even the lookup of `.close` is guarded.
- `HOOK-AWAITABLE-FAIL-CLOSED` — A hook that returns a generator, asyncgen, coroutine or awaitable
  fails closed with `_ZtestNeverExecutedError`.
- `POISONED-RUNNER-NEVER-CLOSED` — A poisoned runner is never closed, because `close()` is an
  unbounded cancel-and-wait on a cancel-resistant task. The reference is
  dropped, the old loop is closed best effort, and the next `_ensure_runner`
  builds a fresh one.
- `XUNIT-RETURN-CONTRACT` — xunit finalizers have a return contract: a generator,
  asyncgen or awaitable result is never driven. It is closed or canceled and
  the teardown unit records an error.
- `POISONED-RUNNER-SKIP-DIAGNOSTIC` — A request against a poisoned runner is skipped with an
  explicit diagnostic instead of surfacing as "Event loop is closed".

## Sticky timer flag

- `STICKY-TIMEOUT-FLAG` — A `SIGALRM` swallowed by an asyncio task step, or by a user
  `except BaseException`, still surfaces as a timeout FAILURE at the accounting
  boundary. The fired flag is sticky across re-arms: `arm` never overwrites it
  and `disarm` never clears it; the reset points are item start and the
  consuming boundary, so every budget unit may surface a swallowed timeout.
- `DISARM-AND-CONSUME-SEPARATE` — Disarm and consume are separate, and the timer re-arms: while the
  gate is open the raise repeats until the consumer disarms, so the xfail and
  skip machinery never matches the flag. The sticky check happens at the
  accounting boundary of every budget unit, before the disarm, and error sinks
  disarm first so the recurring timer is dead before any formatting runs.

## Cleanup dispatch and finalization

- `CLEANUP-DISPATCH` — Cleanups dispatch through `_callMaybeAsync`; error sinks carry kind
  guards; plain `setUp` and `tearDown` follow the result contract; method and
  function forms are symmetric; cleanup callbacks are pre-guarded.
- `XUNIT-TEARDOWN-GUARDS` — xunit `teardown_method` and `teardown_function` finalizers guard the
  kind before the call and the result after it, so a coroutine never reaches the
  drive branch reserved for real async fixtures (`ASYNC-FINALIZERS-ON-RESOLVER-RUNNER`).
- `XUNIT-SHARED-CONTRACT` — `teardown_method` and `teardown_function` share one contract: kind
  guard before, result guard after, symmetric with their setup counterparts.
- `KIND-CLASSIFICATION-OUTER-ONLY` — Kind classification is outer only: `asyncio.run` adapters pass,
  cleanup dispatch is the full `_callMaybeAsync` path, and the `doClassCleanups`
  override is guarded. The CPython pop-one loop is mirrored with per-unit
  budgets, a user override runs as a single budget unit, and lifecycle kind
  guards run before invocation.
- `MULTI-YIELD-IS-TEARDOWN-ERROR` — A second yield from a fixture is a teardown error: the generator is
  driven to exactly one `StopIteration` or `StopAsyncIteration`, `close()` or
  `aclose()` must still run, and its error is chained into the record.
- `FINALIZER-FAILURES-RECORDED` — Finalizer failures of any type, `BaseException` included, are
  recorded and the remaining finalizers still run.
- `HEARTBEAT-DEAD-PEER-SWALLOWED` — A heartbeat that fails because the IPC peer is gone is
  swallowed. The worker is already doomed and the finalizers must still run.
- `INTERRUPT-IDENTITY-PRESERVED` — When `KeyboardInterrupt` escapes `close()` or `aclose()` during
  multi-yield teardown, the teardown-interrupt flag is raised first, so the file
  will stop, and only then is the multi-yield violation wrapped into a
  `RuntimeError`. The identity of the interrupt is not erased.

## Process containment (ADR-004)

- `ADR-004-0` — Group kill is fail closed. `pgid <= 1` never reaches
  `kill(-pgid)`: `pgid = 1` would broadcast `SIGKILL` across the whole uid and
  `pgid = 0` would signal the runner's own group. Both the registry and the kill
  site reject those values. See
  [ADR-004](adr/ADR-004-process-containment.md).
- `ADR-004-1` — Parent-death cascade: the supervisor arms
  `PR_SET_PDEATHSIG(SIGKILL)` in the child between `fork` and `execve`. `fork`
  resets the setting and `execve` preserves it, so a zygote re-arms it on every
  worker it forks, while clean-spawn workers are armed before exec and do not
  re-arm. The re-arm inside the shim covers the window between exec and
  interpreter startup, and `prctl` is idempotent. `getppid` is compared
  immediately after arming, because a parent that died before the arm produces
  no retrospective signal.
- `ADR-004-2` — Zygote EOF watch: EOF or `POLLHUP` on the control
  socket means
  the supervisor is gone, so the zygote kills every live worker group with
  `killpg(SIGKILL)`, including workers it never accepted, and exits. As their
  parent it knows them without a registry. `SIGCHLD = SIG_IGN` is forbidden: a
  zombie pins `pid == pgid` until an exact `waitpid`, and without that pin
  `kill(-pgid)` could reach a reused pid. This layer is best effort when the
  supervisor dies, because the kernel may deliver the parent-death signal before
  the zygote reads EOF; the run watchdog is what holds containment in that
  case.
- `ADR-004-3` — Native run watchdog, one process per run: an mmap
  `PgidRegistry` of 32 slots holding atomic u64 values, a pidfd wait on the
  supervisor, and `killAll` over live slots when it exits. Registration rejects
  `pgid <= 1` and fails open when the registry is full, since 32 slots
  is twice the maximum of 16 concurrent workers and the cascade and EOF layers
  remain. Unregister wins the race against `killAll` (atomic slot reads; a
  double kill is harmless), and the group is killed and unregistered before the
  reap, so the watchdog never signals a pgid freed by a reap. The watchdog is
  untouchable: orphan sweeps and the adoption reaper exclude it globally.
- `ADR-004-BOUNDARY` — `setsid` escapees are covered only while the supervisor
  lives. The orphan sweep catches them; once the supervisor is dead, group
  mechanisms cannot reach a descendant that left its group. This is an accepted
  residual risk. Reaching such processes requires cgroup scoping, which is
  outside the contract of this runner.
- `ADR-004-4` — `getdents64` entries are parsed safely: the flexible `d_name`
  tail is read through a sentinel-pointer cast and `std.mem.span`, never indexed
  as `d_name[0.. :0]`, because the bounds check reads the byte after the
  terminator and panics on the `..` entry. The stat fd is closed on every
  iteration, and a zero `d_reclen` breaks the loop.

## unittest delegation (ADR-005)

- `ADR-005-1` — The lifecycle is delegated: `setUp`, the body, `tearDown`,
  `doCleanups` in LIFO order, `expectedFailure` and skip
  are executed by stock
  `unittest.TestCase.run`, and `IsolatedAsyncioTestCase` by its own runner.
  Capture sits outside `run()`, the private `_callTestMethod` is not used,
  and the verdict is read only from the public result lists. The private
  contacts are a fixed short list, each single-purpose and verdict-free:
  `case._outcome` (write-only re-creation before the safety-net
  `doCleanups` in the common `finally`, whenever the attribute is `None`,
  reset afterwards — the normal path included, not only a control
  exception), `case._callTearDown` (control-exception path),
  `case._testMethodName`, `case._asyncioRunner`/`runner._loop`,
  `cls._class_cleanups`, `unittest.case._module_cleanups`,
  `case._setupAsyncioRunner` (presence check). See
  [ADR-005](adr/ADR-005-unittest-delegation.md).
- `ADR-005-2` — `_wrap_phase` installs interceptors on public instance methods
  and provides per-phase budgets, timestamps and the never-executed sentinel
  `_ztest_body_started`; teardown dominance is carried by the `teardown_timed_out`
  flag consumed in `stopTest`. `__unittest_skip__` and
  `__unittest_expecting_failure__` are copied onto each wrapper, otherwise
  `run()` cannot see them.
- `ADR-005-3` — The primary verdict is derived from the public result lists
  populated by the `addError`, `addFailure`, `addSkip` and `addExpectedFailure`
  hooks. Control exceptions are never swallowed into a green outcome by
  unittest sinks; the outer per-item handler counts them.
- `ADR-005-4` — Function-scope teardown ownership is registered with
  `addCleanup(_resolver_teardown_unit, ...)` before any fixture or hook runs,
  because stdlib `doCleanups` also runs when `setUp` failed. Autouse fixtures
  and conftest setup and teardown hooks run for unittest items too. A
  `case.doCleanups()` safety net in `finally` around `case.run()` covers control
  exception paths and the early return on skip. Exactly-once comes from the pop
  semantics of `_cleanups` plus an idempotent `teardown_scope`. Teardown
  diagnostics are published in an outer `finally` on every path, and a failure
  while preparing a unittest item drains finalizers manually in LIFO order,
  conftest teardown first and then function scope, because the stdlib cleanup
  layer is unreachable there.
- `ADR-005-5` — Lifecycle errors that are not primary, meaning teardown or
  cleanup errors recorded after a primary exception, route to
  `outcome.teardown_errs` from `ZtestResult.addError` and `addFailure`. xfail
  classification is blocked whenever `teardown_errs` is non-empty, on both of
  its paths, so an expected body failure cannot hide an unexpected finalization
  failure and produce a false exit code. Teardown dominance converts an
  otherwise green item into a failure before counter dispatch, and the same rule
  applies on exception paths: xfail and skip branches fail when teardown
  reported errors.
- `ADR-005-6` — The shim `Skipped` exception derives from `BaseException` so a
  user `except Exception` cannot swallow it, and stdlib unittest would route it
  to the error channel. The bridge in `ZtestResult.addError` moves it to the
  skip channel with timeout dominance preserved. An explicit
  `except (Skipped, unittest.SkipTest)` in the item runner catches a skip raised
  during preparation and a native `SkipTest` from `setUpClass`, and the class is
  skipped deterministically through the failed-class cache. The subtest bridge
  routes `Skipped`, `SkipTest` and `XFailed` into sub-skip records.
- `ADR-005-7` — Teardown origin is decided by phase, not by ordering.
  `_PhaseBudgets` keeps a phase stack with `phase` and `phase_off`, and
  `in_teardown_phase()` routes errors from `tearDown`, cleanups and finalizers
  into `teardown_errs` while bypassing the primary slot. Because the phase
  wrapper pops the phase in `finally` before the stdlib `add*` hook runs, the
  exception is tagged inside the wrapper by `_tag_teardown_origin`, and the
  hooks recognize the pre-tag through membership in `teardown_errs`. The subtest
  bridge captures the phase at catch time, while it is still on the stack, and
  teardown subtests go straight to `teardown_errs`. An unexpected teardown error
  is never convertible to xfail; only when a single finalizer exception (or, for
  multiple failing finalizers, their resulting `BaseExceptionGroup`) matches an
  explicit `@pytest.mark.xfail(raises=...)` on the item and no unexpected
  `teardown_errs` remain does xfail conversion apply.
- `ADR-005-8` — Class setup runs under an armed phase budget: `setUpClass` and
  `setup_class` execute inside `budgets.phase("setup_class")` with `phase_off`
  in `finally`, before any user code, so a hung class setup becomes a timeout
  failure.
- `ADR-005-9` — Ownership of xunit teardown follows a successful xunit setup:
  `setup_method` registers `teardown_method`, and `setup_function` registers
  `teardown_function`, before fixture resolution. A successful setup therefore
  gets its paired teardown even if fixtures fail afterwards, while a failed
  setup does not trigger its teardown. A teardown-only hook without a setup is
  legal and registers unconditionally. Conftest teardown hooks register right
  after the resolver owner. LIFO order is preserved: xunit, then conftest, then
  fixture finalizers.
- `ADR-005-10` — Every cleanup callback gets its own budget, applied at
  registration time for `addCleanup` and `addAsyncCleanup` rather than around
  the whole `doCleanups`. A sequence of individually legal cleanups is
  therefore never charged against one shared aggregate budget, and fixture
  finalizers get per-unit budgets through `teardown_scope` (`FINALIZER-PER-UNIT-BUDGET`).
- `ADR-005-11` — pytest-style `setup_class(cls)` and `teardown_class(cls)` are
  plain functions without `@classmethod` and are called arity aware. A bound
  classmethod on a unittest class receives a zero-argument call. Native
  `@unittest.expectedFailure` combined with `subTest` keeps expected semantics:
  a subtest failure inside the body of an expected-failure method is an expected
  failure, and sub-failures are not reported as teardown diagnostics.
- `ADR-005-12` — Coroutine cleanup callbacks keep their async identity:
  `_budgeted_add_cleanup` branches on `inspect.iscoroutinefunction`, and stdlib
  `addAsyncCleanup` keeps delegating to `addCleanup` with no separate override,
  because a sync trampoline around a coroutine produces a never-awaited
  coroutine and a false pass. IPC hardening belongs to the same contract:
  `FORKED` is emitted only after a pipe handshake confirms the child called
  `setpgid(0, 0)`, `register_at_fork` runs before `_connect_ipc_fd199` and
  before user code, and `FD_CLOEXEC` is a second belt on the descriptor.
- `ADR-005-13` — Worker IPC writes are staged, not one-shot. `send_event`
  enqueues a frame into an in-memory buffer (`_obuf`) and returns without a
  syscall when deferred; a flush fires once the buffered frame count, byte
  count, or oldest-frame age crosses its limit, or on a non-deferred send. The
  flush writes the whole buffer in one coalesced cycle, in chunks of at most
  262144 bytes, and keeps retrying through partial writes. fd 199 is in
  `O_NONBLOCK`; `SIGALRM` and `SIGINT` are blocked before the write loop, and
  the mask stays held across the whole cycle of successful partial writes,
  including the Python accounting between syscalls. The mask is lifted only
  for the duration of a backpressure wait for `POLLOUT` under an absolute
  30 s deadline and re-acquired right after the wait, so the per-test timeout
  keeps firing under backpressure; the original mask is restored when the
  flush returns. A frame interrupted mid-write makes the channel terminal:
  further sends are skipped and the receiver fails closed on the CRC
  mismatch.

## OSS suite parity (0.5.0)

Invariants governing compatibility with real-world pytest suites under ztest. Each entry protects a piece of pytest
semantics that diverges silently when broken.

- `OSS-INI` — ini configuration is read from disk in pytest's precedence order:
  `pytest.ini` > `pyproject.toml [tool.pytest.ini_options]` > `tox.ini [pytest]`.
  Supported keys include `plugins`, `filterwarnings` (applied per item; the last
  ini line wins) and `python_files` (replaces the built-in discovery patterns
  entirely, matching pytest). Plugins from `plugins=` and conftest
  `pytest_plugins` load after conftests and sit before descendants in the
  lookup chain. `config.stash` is a shared dict keyed by `StashKey` instances.
- `OSS-DISCOVERY` — `python_files` from ini drives supervisor-side discovery
  (glob `*` matched against the file name; the config scan is targeted, not a
  full TOML parse).
- `OSS-CUSTOM-COLLECT` — `pytest_collect_file` (firstresult, leaf-first) handles
  non-Python files below any conftest declaring the hook. `pytest.File` and
  `pytest.Item` are minimal Node bases with a cooperative `from_parent` and a
  fallback for non-cooperative `__init__(self, name, parent)`. `fspath` follows
  py.path shape (string concatenation via `__add__`). Collect-only mode
  publishes node ids through `COLLECT_BATCH`, and
  `pytest_runtest_teardown(item, nextitem)` fires for every custom item. The
  supervisor emits non-`.py` files only below directories whose ancestor
  conftest declares the hook (text probe; the scope inherits down the tree).
- `OSS-IDS` — parametrized ids follow pytest's IdMaker: value ids for
  str/int/float/bool/None/complex (strings fully `unicode_escape`d, control
  characters included), bytes ascii-escaped without the `b''` wrapper,
  `re.Pattern` by pattern, enums as `str(value)` (default `Class.MEMBER`),
  carriers of `__name__` by name, other objects positional
  (`{argname}{index}`). Duplicate ids are suffixed numerically on every
  occurrence, with a `_` separator when the id ends in a digit (`dup0`,
  `1_0`). Stacked decorators apply bottom-first (the bottom decorator owns the
  outer axis). A tuple value for a single argname gets a positional id, not
  unpacking. List/tuple argnames enable pytest's force-tuple behavior.
  A name/value count mismatch is a loud collection error, never silent
  truncation.
- `OSS-ENV` — `os.environ["PYTHONPATH"]` is never written; the import path
  lives in the worker's `sys.path` only, so CLI subprocesses spawned by tests
  inherit a clean environment (pytest parity).
- `OSS-FD-CAPTURE` — file descriptors 1 and 2 are redirected per item
  (`os.dup2` into temp files) so subprocess output joins the capture instead of
  interleaving with the supervisor's verbose stream; the fd buffer merges into
  the capture at the item boundary (mid-item reads of the fd buffer through
  capsys are not supported — documented approximation). The worker IPC channel
  runs on a dedicated fd and is unaffected. A partial dup2 failure restores the
  original descriptors before any close.
- `OSS-PKGUTIL` — loaded test modules get a real SourceFileLoader-backed
  `__spec__`, so `pkgutil.get_data(__name__, rel)` works.
- `OSS-IGNORE` — conftest `collect_ignore` (paths relative to the conftest; a
  directory entry ignores its subtree) and `collect_ignore_glob` (fnmatch on the
  absolute path) are consulted before module import.
- `OSS-PARAMS` — direct and indirect parametrization of a name whose fixture
  has its own `params=` replaces the fixture axis instead of multiplying it.
- `OSS-SCOPE` — a fixture never depends on a narrower scope
  (function < class < module < package < session); violations fail loudly with
  ScopeMismatch (pytest parity).
- `OSS-DUP-GATE` — parametrizing the same name twice is a loud collection error
  across all source pairs (decorator/decorator, hook/hook, module hook).
- `OSS-CLOSURE` — the fixturenames closure includes autouse fixtures and their
  transitive dependencies breadth-first; `*args`/`**kwargs` parameters are never
  fixture names and are never resolved.
- `OSS-MRO` — fixture overrides in the class MRO (method resolution order)
  resolve most-derived first
  (`reversed(getmro)`); classmethod fixtures rebind to their own class.
- `OSS-COLOR` — ANSI color is emitted
  only on a real tty and honors
  `NO_COLOR`; piped output stays machine-parseable (pytest parity).
- `OSS-RH` — reporting metadata hooks (`pytest_report_header` and peers) are
  recognized and deliberately NOT called: ztest prints no pytest header, the
  hook has no observable effect, and its presence in a conftest never fails
  collection.
