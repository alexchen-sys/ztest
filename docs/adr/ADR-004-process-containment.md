# ADR-004: Kernel-level process containment

- Status: Accepted
- Type: architecture decision record (ADR)
- Applies to: 0.5.0 and later; Darwin parity added in 0.6.0
- Related: [invariant registry](../INVARIANTS.md), [ADR-005](ADR-005-unittest-delegation.md)

## Context

A test runner has a stronger obligation than a normal parent process: when a run
ends, or when the runner itself dies, nothing it started may keep running.

Tests break this assumption in ordinary ways. A test starts a subprocess and
never waits for it. A test spawns a shell that spawns a daemon. A test calls
`exec` on an interpreter that then blocks on a socket, so there is no Python
frame left to run `atexit` handlers or a `finally` block. A test hangs inside a
C extension and never returns to the interpreter loop. A CI job kills the runner with `SIGKILL`, because the job hit its own
wall-clock limit.

Process-wide cleanup written in Python is not sufficient for any of those
cases. Signal handlers do not run in a `SIGKILL`ed process, and they do not run
in a process that is blocked inside native code. A watchdog thread inside the
worker dies with the worker. The only reliable actors are the kernel and a
process that the test cannot reach, cannot kill, and cannot make dependent on
the worker staying healthy.

Three properties have to hold at the same time:

1. If the worker's parent chain disappears, the worker and its descendants die
   without cooperation from any interpreter.
2. If the supervisor dies abruptly, all worker process groups die, including
   workers that were forked but not yet accepted, and including groups whose
   leader was replaced by `exec`.
3. Whatever mechanism enforces 1 and 2 must never signal a process group it
   does not own. A wrong `killpg` is worse than a leaked process: it can take
   down the runner, the invoking shell, or an unrelated job on the same uid.

## Decision

Containment is implemented in three independent layers. Each layer can fail or
be degraded without the others becoming unsound, and each layer is written so
that a missed kill is a leak, never a misdirected kill.

### Layer 1: parent-death cascade

The supervisor arms `PR_SET_PDEATHSIG(SIGKILL)` on the zygote and on every
clean-spawn worker it creates, inside the child branch, between `fork` and
`execve`. The run watchdog is the one deliberate exception: it must outlive
the supervisor's death long enough to sweep, so it is never armed.

- `fork` resets the parent-death signal for the child.
- `execve` preserves it, so an `exec`'d interpreter keeps the contract.
- The arm order differs by spawn path. Zygote-forked workers call `setpgid`
  first and re-arm `pdeathsig` second, inside the child branch: the group must
  exist before the death contract is armed, because a later `killpg` addresses
  the group, not the process. Clean-spawn workers invert the order: they are
  armed before `execve`, and their group is created afterwards — supervisor-side
  immediately after the exec handshake, and again as the first statement of the
  Python entry point — with no re-arm, because the pre-`execve` arm survives
  `execve`.

Each worker the zygote forks re-arms the setting itself, in its own child
branch, for the same reason: the worker is a new process and inherits nothing.
Clean-spawn workers, which the supervisor forks itself, are armed before
`execve` and do not re-arm. The shim re-arms once more on the zygote side to
cover the window between `execve` and interpreter startup; `prctl` is
idempotent, so a redundant arm is harmless.

`getppid` is compared immediately after arming. If the parent already died, the
kernel will not deliver a retrospective signal, so the child exits on the spot
with status 125 instead of continuing as an orphan.

The error pipe carries at most one message: four bytes of `errno`, written
only after a failed `execve`. A successful `execve` closes the pipe through
`CLOEXEC`, and the parent reads EOF, takes the pid and treats the handshake
as complete. An early exit during arming (status 125) also closes the pipe
without `errno`, which the parent cannot distinguish from a successful exec;
a child lost that way surfaces later as a worker that never connects, not as
a spawn failure.

This layer needs no bookkeeping and cannot be defeated by a hung interpreter,
but it covers exactly the processes the runner armed: the zygote, its workers,
and the clean-spawn workers. `fork` resets the parent-death signal, so a
descendant that test code forks or `exec`s into existence inherits no contract —
the cascade does not follow an arbitrary descendant tree. While the supervisor
is alive, such descendants are held by the worker group kill and, after a
`setsid` escape, by the orphan sweep under the supervisor's child-subreaper
role; the residual boundary is recorded as `ADR-004-BOUNDARY` below.

### Layer 2: zygote EOF watch

The zygote holds one control socket to the supervisor. When the supervisor
dies, the kernel closes its end, and the zygote sees EOF or `POLLHUP` on that
socket. At that point the zygote sends `SIGKILL` to every live worker process
group with `killpg` and exits.

The zygote can do this without a registry because it is the parent of every
worker it forked: it knows the pids, including workers that were forked but
never accepted by the supervisor. This is the layer that covers the gap the
supervisor's own bookkeeping cannot cover, because bookkeeping stops the moment
the supervisor is gone.

Two details matter:

- `SIGCHLD = SIG_IGN` is forbidden. With that disposition the kernel reaps
  children silently, and a reaped group leader frees `pid == pgid` for reuse;
  a later `kill(-pgid)` could then hit a process that merely inherited the pid.
  Explicit `waitpid` keeps the zombie, and the zombie pins the pid.
- The watch is best effort by nature. The kernel can deliver the layer 1
  parent-death signal to the zygote before it gets to read the socket. In that
  case the zygote is already gone and cannot sweep, which is exactly why layer
  3 exists and why the run watchdog, not the zygote, is the carrier of
  containment when the supervisor dies.

### Layer 3: native run watchdog

One watchdog process per run, forked by the supervisor before workers start.

- It immediately calls `setpgid(0, 0)` and `close_range(3, ...)`, keeping only
  stdio and a pidfd for the supervisor. It lives in its own process group, so
  neither a test's `killpg` nor a sweep of worker groups can reach it.
- It opens a pidfd for the supervisor and blocks in `poll` on it. `POLLIN` on a
  pidfd means the process has exited; this works for a zombie state as well,
  and there is no polling loop on a bare pid, because a reused pid would make
  the watchdog signal an unrelated group.
- On wakeup it walks a shared registry and calls `killpg(SIGKILL)` for every
  occupied slot, then exits.

The registry is an anonymous `MAP_SHARED` mapping of 32 slots of atomic `u64`,
256 bytes in total, created before the fork and visible to both processes. A
slot holds a `pgid`, with `0` meaning free.

Rules that make the registry safe:

- **Fail closed on `pgid <= 1`.** `pgid = 1` under `kill(-pgid)` is `kill(-1)`,
  a broadcast `SIGKILL` to every process the user may signal. `pgid = 0` is the
  caller's own group. Neither is ever a legal worker group, since a worker group
  always has `pgid == pid > 1`. Both the registry and the kill site reject those
  values, so a corrupted or reused value cannot turn into a broadcast.
- **Fail open on overflow.** Registration returns failure when all slots are
  occupied. Capacity is twice the maximum of 16 concurrent workers, so overflow
  requires more parallelism than the supervisor allows; if it happens, the
  caller drops the result, containment of that worker degrades silently, and
  the cascade and EOF layers still run as the remaining containment net.
- **Registration is idempotent.** Registering the same `pgid` twice does not
  consume a second slot, and unregistering frees exactly one.
- **Registration happens before accept.** For zygote workers, the supervisor
  registers on parsing the `FORKED` reply, not when the worker connects. The
  zygote called `setpgid` before replying, so `pgid == pid` already holds at
  that point, and the window "worker is alive but not registered" is closed.
  For clean-spawn workers, registration happens immediately after `setpgid`.
- **Unregister wins the race against `killAll`.** Slots are read and written
  atomically. If the watchdog is already walking the registry while the
  supervisor unregisters, both may act on the same slot; a second `SIGKILL` to
  a group that has already been killed is harmless, and the pid cannot be reused
  in between because the group is killed and unregistered before the reap.
- **Kill and unregister before reap.** Phase 4 of the run kills the worker
  group, unregisters the slot, and only then collects the exit status. Reaping
  first would free the pid while the slot still named it, which is the exact
  reuse hazard the registry exists to avoid.
- **The watchdog is untouchable.** Orphan sweeps and the adoption reaper exclude
  it through a global exclusion, and the reaper also detects already-seen pids
  so that a `WNOWAIT`-peeked zombie does not make a sweep loop. Without the
  exclusion, a sweep would kill the containment layer it is running under, and
  the final deferred teardown would then signal a reused pid.

## Darwin parity (0.6.0)

The three-layer contract is unchanged; Darwin supplies different kernel
primitives. `src/pal/mod.zig` exposes one interface and `src/pal/darwin.zig`
implements it. The Linux names in the preceding sections describe the Linux
backend, not a claim that Darwin emulates those syscalls.

- **Parent-death cascade.** Darwin has no `PR_SET_PDEATHSIG` or child
  subreaper. The parent registers `EVFILT_PROC` with `NOTE_EXIT` on the
  protected parent process (zygote for a forked worker, supervisor for a
  clean-spawn worker), and kills the child's process group when that parent
  exits. The existing `getppid` check after arming remains the race guard for
  a parent that has already exited. `NOTE_FORK | NOTE_TRACK` tracks descendants
  created by the protected zygote rather than replacing its death watch.
  Process-group kills and the `pgid <= 1` fail-closed rule are identical on
  both systems.
- **Supervisor-death watchdog.** The run watchdog waits for the supervisor
  with kqueue `EVFILT_PROC`/`NOTE_EXIT` instead of `pidfd_open` plus `poll`.
  It still uses the same anonymous `MAP_SHARED` `PgidRegistry`, registers
  groups before accept, and kills and unregisters before reap. This is a
  kernel notification of the watched process, not a bare-pid polling loop.
- **Peer identity.** Unix-socket authentication uses `LOCAL_PEERCRED` where
  the credential structure is available and `LOCAL_PEERPID` for the direct
  peer-pid path, replacing Linux `SO_PEERCRED`. The accepted peer must still
  satisfy `getpgid(zygote_pid) == zygote_pid`; socket credentials do not
  replace the group-identity check.
- **Descriptor and process inspection.** `/dev/fd/1` replaces
  `/proc/self/fd/1` when classifying captured output. Child enumeration uses
  `proc_listchildpids`, and `proc_pidinfo` supplies process status and
  parent/group fields for sweeps. Darwin does not adopt escaped descendants
  through `PR_SET_CHILD_SUBREAPER`, so the Linux adoption sweep has no direct
  equivalent; `ADR-004-BOUNDARY` remains explicit rather than being presented
  as solved by libproc.
- **Resource accounting.** `getrusage(RUSAGE_CHILDREN)` is available through
  the same PAL boundary, but `ru_maxrss` is bytes on Darwin and KiB on Linux.
  The backend normalizes that unit before reporting; CI must not compare a raw
  Darwin value with a raw Linux value.

The zygote EOF watch, CRC-framed nonblocking IPC, in-worker timer, outside
inactivity window, and exact-wait/zombie identity rules remain platform
invariants. kqueue replaces the process-lifecycle wait mechanism, not the
containment policy or its fail-closed accounting.

## Invariants

The registry in [INVARIANTS.md](../INVARIANTS.md) carries the normative
statements under the `ADR-004-*` keys. The critical ones are:

- `ADR-004-0` — group kill is fail closed; `pgid <= 1` never reaches
  `kill(-pgid)`, at the registry or at the kill site.
- `ADR-004-1` — the parent-death cascade arms `SIGKILL` pre-exec, and the
  `getppid` check runs immediately after arming.
- `ADR-004-2` — EOF on the control socket makes the zygote kill all worker
  groups, and `SIGCHLD = SIG_IGN` remains forbidden.
- `ADR-004-3` — one run watchdog per run, registration before accept,
  unregister winning the race, kill and unregister before reap, and the watchdog
  excluded from every sweep.
- `ADR-004-4` — directory entries are parsed through `std.mem.span` on the
  flexible tail; the stat descriptor is closed per iteration and a zero
  `d_reclen` terminates the loop.

## Alternatives considered

**cgroup v2 per run.** A cgroup would give exact subtree containment with a
single `cgroup.kill`, and it is the only mechanism that reaches a descendant
that called `setsid` and reparented away. It requires delegated cgroup
permissions or root on every host where the runner executes, which is not
available on shared build machines and adds a host-dependent failure mode to a
tool that must run anywhere Python runs. Rejected for the supported contract,
documented as the mechanism that would remove the residual risk below.

**Per-worker Python watchdog.** A forked supervisor inside the worker's own
process tree is cheap to write but shares the worker's fate: it dies when the
worker is `SIGKILL`ed or when the interpreter is replaced by `exec`, and it adds
one process per worker instead of one per run. Rejected.

**`SIGCHLD = SIG_IGN` in the zygote.** Reaping without bookkeeping looks
simpler, and it is unsafe for the reason described above: pids become reusable
while group-scoped kills can still be addressed to them. Rejected.

**Polling the supervisor pid.** Reading `/proc` or calling `kill(pid, 0)` in a
loop is a race against pid reuse, and it burns a wakeup budget for the whole run.
The pidfd is the supported way to wait for a process to die. Rejected.

## Consequences

Positive:

- Containment works when the runner is `SIGKILL`ed, when a worker hangs in
  native code, and when a test `exec`s a different interpreter. None of those
  cases need cooperation from Python.
- A misdirected broadcast is structurally impossible: the fail-closed guards
  reject the two dangerous pgid values at both the producer and the consumer of
  the registry.
- The cost is fixed: one extra process and 256 bytes of shared memory per run,
  independent of test count and worker count.

Negative and accepted:

- Three layers with overlapping responsibilities mean three code paths to keep
  correct. The registry rules (registration before accept, idempotent register,
  unregister before reap, watchdog exclusion) are the parts that must not be
  "simplified" later; each one closes a specific reuse or race window.
- When the watchdog cannot be spawned or its mapping cannot be created, the run
  continues with layers 1 and 2 and reports the degraded containment on stderr.
  That is a deliberate degradation, not a silent one.
- `pdeathsig` is cleared when a process changes its credentials through a
  set-user-id `exec`. A test that does that leaves the cascade, and is covered
  only by group mechanisms while the supervisor lives.

## Residual risk

A descendant that calls `setsid` and outlives the supervisor is outside the
reach of group-based containment. While the supervisor is alive, the orphan
sweep finds such processes and kills them. After the supervisor is dead, no
layer can address them: layer 1 arms only the runner's own processes, and such
a descendant never inherited the contract; layer 2 has no record of it; layer 3
only holds process groups.

This is an accepted limitation of the contract, recorded as
`ADR-004-BOUNDARY`. Closing it requires cgroup scoping per run, which is a
different execution model with different privileges.
