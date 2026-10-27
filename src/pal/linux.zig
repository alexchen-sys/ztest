//! Linux backend of the ztest platform abstraction layer.
//!
//! Every ADR-004 containment primitive that is Linux-specific lives here: pidfd process
//! handles, PR_SET_PDEATHSIG / PR_SET_CHILD_SUBREAPER, SO_PEERCRED peer authentication,
//! /proc enumeration (task children, /proc/<pid>/stat starttime) and /proc/self/fd output
//! channel duplication. The invariants are documented next to the primitives they protect;
//! main.zig consumes only the pal.mod interface and must not grow a raw syscall again.

const std = @import("std");
const linux = std.os.linux;
const builtin = @import("builtin");

pub const fd_t = linux.fd_t;

// siginfo_t.si_pid sits at offset 16 on 64-bit Linux (x86_64 and aarch64:
// si_signo i32 [0..4], si_errno i32 [4..8], si_code i32 [8..12], _pad0 i32 [12..16], si_pid i32 [16..20]).
// On any other architecture compilation must fail rather than silently reading the wrong offset.
comptime {
    if (builtin.os.tag != .linux) {
        @compileError("pal/linux.zig must only be imported when targeting Linux (use pal.mod)");
    }
    if (builtin.cpu.arch != .x86_64 and builtin.cpu.arch != .aarch64) {
        @compileError("si_pid offset 16 verified for x86_64 and aarch64 Linux only");
    }
}

fn siginfoPid(buf: *const [128]u8) i32 {
    return std.mem.readInt(i32, buf[16..20], .little);
}

pub fn monotonicMs() i64 {
    var ts: linux.timespec = .{ .sec = 0, .nsec = 0 };
    _ = linux.clock_gettime(.MONOTONIC, &ts);
    return @as(i64, ts.sec) * 1000 + @divTrunc(@as(i64, ts.nsec), 1_000_000);
}

/// Enables PR_SET_CHILD_SUBREAPER (36) so orphaned Zygote workers and reparented grandchildren
/// are adopted by this supervisor and can be reaped via waitPidWithDeadline.
pub fn enableChildSubreaper() void {
    _ = linux.prctl(@backingInt(linux.PR.SET_CHILD_SUBREAPER), 1, 0, 0, 0);
}

/// Outcome of the process-group containment check for an authenticated peer.
pub const PgidCheck = enum {
    /// getpgid(pid) == pid verified right now (live group leader).
    leader,
    /// ESRCH: the peer already exited and was reaped. Sound to accept: in both shim worker paths the
    /// worker calls setpgid(0,0) before _connect_ipc_fd199, so an authenticated connection from
    /// worker_pid proves the invariant held at connect time. A dead peer needs
    /// no containment; Phase 4 recovers its real status via REAP/waitPidWithDeadline.
    dead_or_reaped,
    /// Live peer in a foreign process group: containment invariant violated -> the peer is rejected.
    violator,
};

/// Verifies the process-group invariant `getpgid(pid) == pid` via raw `linux.syscall1(.getpgid, ...)`.
/// One best-effort `setpgid` retry is attempted (only legal pre-exec) before declaring a violator.
/// A fully-reaped peer (ESRCH) is reported separately so the accept
/// path does not false-crash fast workers that already exited (connection-after-setpgid ordering in
/// the shim guarantees the invariant held at connect time).
pub fn checkPeerPgid(pid: i32) PgidCheck {
    if (pid <= 0) return .violator;
    const upid: usize = @intCast(pid);
    const rc = linux.syscall1(.getpgid, upid);
    if (linux.errno(rc) == .SUCCESS and rc == upid) return .leader;
    if (linux.errno(rc) == .SRCH) return .dead_or_reaped;
    // Not a leader (or transient error): one best-effort pre-exec setpgid retry, then re-check.
    _ = linux.setpgid(pid, pid);
    const rc2 = linux.syscall1(.getpgid, upid);
    if (linux.errno(rc2) == .SUCCESS and rc2 == upid) return .leader;
    if (linux.errno(rc2) == .SRCH) return .dead_or_reaped;
    return .violator;
}

/// pidfd_open(2) via raw syscall: a pidfd pins the worker's underlying `struct pid`
/// (not the numeric pid — the number is released for reuse the moment the process is reaped) and
/// becomes readable (POLLIN) the moment the process EXITS, zombie state included. Reuse-safety of
/// our kill signals comes from the pinned zombie plus the kill(-pgid)-before-reap ordering, not
/// from the pidfd. Returns -1 on any failure (caller degrades to the deadline-only behavior).
pub fn pidfdOpen(pid: i32) fd_t {
    if (pid <= 0) return -1;
    const rc = linux.syscall2(.pidfd_open, @as(usize, @intCast(pid)), 0);
    if (linux.errno(rc) != .SUCCESS) return -1;
    return @intCast(rc);
}

/// Watchdog death-source fd: on Linux the supervisor's own pidfd; poll(POLLIN) on it fires the
/// moment the supervisor dies. Kept as a separate function so the Darwin backend can substitute
/// its kqueue(EVFILT_PROC) handle without changing spawnBatchWatchdog's fork layout.
pub fn openSupervisorDeathFd(pid: i32) fd_t {
    return pidfdOpen(pid);
}

/// Closes every descriptor except 0/1/2 and `keep_fd` in the freshly forked watchdog: on Linux
/// this is one close_range(2) call pair, so no inherited socket ever survives into the watchdog.
pub fn closeAllFdsExcept(keep_fd: fd_t) void {
    _ = linux.close_range(@as(fd_t, @intCast(keep_fd + 1)), std.math.maxInt(fd_t), .{ .CLOEXEC = false, .UNSHARE = false });
    if (keep_fd > 3) {
        _ = linux.close_range(3, @as(fd_t, @intCast(keep_fd - 1)), .{ .CLOEXEC = false, .UNSHARE = false });
    }
}

/// Selectively reaps zombie children adopted via PR_SET_CHILD_SUBREAPER, without recording
/// statuses: the kernel recycles a pid the moment it is reaped, so a bare-pid status map could
/// hand Phase 4 a stale entry for a colliding reused pid and fabricate a false CRASHED(9).
/// Registered (protected) worker zombies are never reaped here — active slots still signal those
/// pids (`kill(-pid)` group sweeps) until their Phase 4 transition, and reaping would release the
/// pid for reuse. The sweep peeks with WNOWAIT and reaps only unregistered adoptees; a bounded
/// seen-set breaks the peek cycle when only protected zombies remain.
///
/// `protected_wd_pid` is the live run watchdog: the sweep never reaps it (the runMain defer
/// owns that); the seen-set breaks the cycle, else a WNOWAIT peek of a zombie watchdog would
/// loop the sweep. main.zig passes its g_wd_pid here (0 = none).
pub fn reapAdoptedChildren(protected: []const i32, protected_wd_pid: i32) void {
    var seen_buf: [256]i32 = undefined;
    var seen_n: usize = 0;
    while (true) {
        var siginfo_buf: [128]u8 align(8) = @splat(0);
        const prc = linux.syscall5(
            .waitid,
            0, // P_ALL
            0,
            @intFromPtr(&siginfo_buf),
            linux.W.EXITED | linux.W.NOHANG | linux.W.NOWAIT,
            0,
        );
        switch (linux.errno(prc)) {
            .SUCCESS => {},
            .INTR => continue,
            else => return, // ECHILD: no children at all
        }
        const pid = siginfoPid(&siginfo_buf);
        if (pid <= 0) return; // no zombie available right now
        if (pid == protected_wd_pid or pidInList(protected, pid)) {
            if (pidInList(seen_buf[0..seen_n], pid)) return; // full cycle: only protected zombies left
            if (seen_n >= seen_buf.len) return;
            seen_buf[seen_n] = pid;
            seen_n += 1;
            continue;
        }
        // Unregistered adoptee: reap it for real (pid-specific, never a blind wait4(-1)). The
        // raw status is deliberately not recorded anywhere: only
        // unregistered pids are reaped here, so no Phase 4 lookup can ever depend on it.
        var st: i32 = -1;
        while (true) {
            const wrc = linux.wait4(pid, &st, 0, null);
            switch (linux.errno(wrc)) {
                .SUCCESS => break,
                .INTR => continue,
                else => {
                    st = -1; // ECHILD race: already reaped elsewhere
                    break;
                },
            }
        }
        seen_n = 0; // progress made: reset cycle detection
    }
}

pub fn pidInList(list: []const i32, pid: i32) bool {
    for (list) |p| {
        if (p == pid) return true;
    }
    return false;
}

pub const LinuxDirent64 = extern struct {
    d_ino: u64,
    d_off: i64,
    d_reclen: u16,
    d_type: u8,
    d_name: [1]u8,
};

/// Audits `/proc/<pid>/task` via `linux.getdents64` (c_uint length in Zig 0.17.0).
pub fn countProcTasks(pid: i32) !usize {
    var path_buf: [64]u8 = @splat(0);
    const path_z = try std.fmt.bufPrintSentinel(&path_buf, "/proc/{d}/task", .{pid}, 0);

    const open_rc = linux.open(path_z.ptr, .{ .ACCMODE = .RDONLY, .DIRECTORY = true, .CLOEXEC = true }, 0);
    switch (linux.errno(open_rc)) {
        .SUCCESS => {},
        else => return error.ProcTaskOpenFailed,
    }
    const dir_fd: fd_t = @intCast(open_rc);
    defer _ = linux.close(dir_fd);

    var dirent_buf: [4096]u8 align(@alignOf(LinuxDirent64)) = @splat(0);
    var task_count: usize = 0;

    while (true) {
        const nread_rc = linux.getdents64(dir_fd, &dirent_buf, @intCast(dirent_buf.len));
        switch (linux.errno(nread_rc)) {
            .SUCCESS => {},
            else => return error.GetdentsFailed,
        }
        if (nread_rc == 0) break;

        var bpos: usize = 0;
        while (bpos < nread_rc) {
            const d: *const LinuxDirent64 = @ptrCast(@alignCast(&dirent_buf[bpos]));
            if (d.d_reclen == 0) break;
            const name_ptr: [*:0]const u8 = @ptrCast(&d.d_name[0]);
            const name = std.mem.span(name_ptr);
            if (!std.mem.eql(u8, name, ".") and !std.mem.eql(u8, name, "..")) {
                task_count += 1;
            }
            bpos += d.d_reclen;
        }
    }
    return task_count;
}

/// Scans /proc for processes whose PPID == `self_pid` (children adopted via PR_SET_CHILD_SUBREAPER after a
/// Zygote death, including never-registered fork-race workers) and SIGKILLs + reaps every one not in
/// `exclude`. A process whose PGRP belongs to a protected worker is spared: that group is the
/// worker's own domain and is swept by the worker's own teardown. `exclude` guards by bare PID,
/// `protected_groups` by PGRP. The call is bounded by a 10 s budget: past it, remaining adoptees
/// are still SIGKILLed but not reaped.
///
/// Candidate enumeration is `/proc/self/task/<pid>/children` (CONFIG_PROC_CHILDREN): under
/// PR_SET_CHILD_SUBREAPER every adopted orphan is a direct child of the supervisor, so the kernel
/// reports exactly the candidate set — O(children) instead of O(processes). A read failure falls
/// back to a full /proc scan.
///
/// Ownership discriminator is the kernel start time (field 22 of /proc/<pid>/stat, jiffies):
/// a candidate strictly older than the oldest live worker predates every live worker and is a
/// leftover of a finished one; anything younger may be a live sibling's helper and is spared.
/// `older_than_ticks` is that threshold; `null` selects kill-everything-not-protected for the
/// quiescent/final sweeps and the Zygote teardown.
///
/// `protected_wd_pid` is the live run watchdog pid (main.zig's g_wd_pid): every sweep must
/// spare it, otherwise the very first per-slot sweep or killAndCloseZygote would reap it
/// (ppid == supervisor, own pgrp), and the final defer would then fire SIGKILL at an
/// already-reaped (recycled) pid.
pub fn killAdoptedOrphans(
    self_pid: i32,
    exclude: []const i32,
    protected_groups: []const i32,
    older_than_ticks: ?u64,
    protected_wd_pid: i32,
) void {
    const budget_deadline: i64 = monotonicMs() + 10_000;
    // direct-children primary path: O(children) through /proc/self/task/<pid>/children.
    var children_buf: [8192]u8 = undefined;
    if (sweepAdoptedViaProcChildren(self_pid, exclude, protected_groups, budget_deadline, older_than_ticks, protected_wd_pid, &children_buf)) return;

    // Fallback (direct-children): full /proc scan.
    const open_rc = linux.open("/proc", .{ .ACCMODE = .RDONLY, .DIRECTORY = true, .CLOEXEC = true }, 0);
    if (linux.errno(open_rc) != .SUCCESS) return;
    const dir_fd: fd_t = @intCast(open_rc);
    defer _ = linux.close(dir_fd);

    var dirent_buf: [8192]u8 align(@alignOf(LinuxDirent64)) = @splat(0);
    while (true) {
        const nread_rc = linux.getdents64(dir_fd, &dirent_buf, @intCast(dirent_buf.len));
        if (linux.errno(nread_rc) != .SUCCESS or nread_rc == 0) break;

        var bpos: usize = 0;
        while (bpos < nread_rc) {
            const d: *const LinuxDirent64 = @ptrCast(@alignCast(&dirent_buf[bpos]));
            if (d.d_reclen == 0) break;
            const name_ptr: [*:0]const u8 = @ptrCast(&d.d_name[0]);
            const name = std.mem.span(name_ptr);
            bpos += d.d_reclen;

            const pid = std.fmt.parseInt(i32, name, 10) catch continue;
            sweepOneAdoptee(pid, self_pid, exclude, protected_groups, budget_deadline, older_than_ticks, protected_wd_pid);
        }
    }
}

/// direct-children: enumerate the supervisor's direct children through the CONFIG_PROC_CHILDREN
/// file and run the standard sweep on each. Returns true when the enumeration succeeded (an EMPTY
/// file is a success: "no children" is a valid answer, not an error), false when the caller must
/// fall back to the full /proc scan.
///
/// Why this is sound: PR_SET_CHILD_SUBREAPER makes the supervisor the reaper of every orphaned
/// descendant, so a reparented grandchild / FORK-race worker appears in this list by definition.
/// The file is per-thread, but adoption attaches to the thread-group leader (find_new_reaper ->
/// find_alive_thread) and the supervisor forks from that same main thread, so the single
/// `<self_pid>` task dir covers everything; were the process ever multi-threaded, we fall back to
/// the full scan instead of silently enumerating a subset.
fn sweepAdoptedViaProcChildren(
    self_pid: i32,
    exclude: []const i32,
    protected_groups: []const i32,
    budget_deadline: i64,
    older_than_ticks: ?u64,
    protected_wd_pid: i32,
    buf: []u8,
) bool {
    const thread_count = countProcTasks(self_pid) catch return false;
    if (thread_count != 1) return false;

    var path_buf: [64]u8 = @splat(0);
    const path = std.fmt.bufPrintSentinel(&path_buf, "/proc/self/task/{d}/children", .{self_pid}, 0) catch return false;
    const fd_rc = linux.open(path.ptr, .{ .ACCMODE = .RDONLY, .CLOEXEC = true }, 0);
    if (linux.errno(fd_rc) != .SUCCESS) return false;
    const fd: fd_t = @intCast(fd_rc);
    defer _ = linux.close(fd);

    var total: usize = 0;
    var saw_eof = false;
    while (total < buf.len) {
        const rd_rc = linux.read(fd, buf[total..].ptr, buf.len - total);
        switch (linux.errno(rd_rc)) {
            .SUCCESS => {},
            .INTR => continue,
            else => return false, // read error: fall back to the full scan
        }
        if (rd_rc == 0) {
            saw_eof = true;
            break;
        }
        total += @intCast(rd_rc);
    }
    // Buffer full without EOF: the last pid may be truncated mid-digits, and a truncated pid is a
    // different pid — never guess, fall back to the full scan (a supervisor with >1000 direct
    // children is already far outside any supported topology).
    if (!saw_eof) return false;

    var it = std.mem.tokenizeAny(u8, buf[0..total], " \n\t");
    while (it.next()) |tok| {
        const pid = std.fmt.parseInt(i32, tok, 10) catch continue;
        sweepOneAdoptee(pid, self_pid, exclude, protected_groups, budget_deadline, older_than_ticks, protected_wd_pid);
    }
    return true;
}

/// One candidate of the orphan sweep. Shared by the direct-children path and
/// the full-/proc fallback, so the two enumerations can never drift apart in
/// filtering.
fn sweepOneAdoptee(
    pid: i32,
    self_pid: i32,
    exclude: []const i32,
    protected_groups: []const i32,
    budget_deadline: i64,
    older_than_ticks: ?u64,
    protected_wd_pid: i32,
) void {
    if (pid <= 0 or pid == self_pid) return;
    if (pid == protected_wd_pid) return; // the run watchdog is untouchable here (spared by every sweep)
    if (pidInList(exclude, pid)) return;

    // Parse /proc/<pid>/stat: fields after the last ')' are: state(1) ppid(2) pgrp(3)...
    var stat_path_buf: [64]u8 = @splat(0);
    const stat_path = std.fmt.bufPrintSentinel(&stat_path_buf, "/proc/{d}/stat", .{pid}, 0) catch return;
    const sfd_rc = linux.open(stat_path.ptr, .{ .ACCMODE = .RDONLY, .CLOEXEC = true }, 0);
    // Race: a pid listed in `children` may already be gone by the time we stat it (reaped
    // elsewhere) -> ENOENT -> skip, exactly the degradation the full scan always had.
    if (linux.errno(sfd_rc) != .SUCCESS) return;
    const sfd: fd_t = @intCast(sfd_rc);
    var stat_buf: [512]u8 = @splat(0);
    const rd_rc = linux.read(sfd, &stat_buf, stat_buf.len);
    _ = linux.close(sfd);
    if (linux.errno(rd_rc) != .SUCCESS or rd_rc <= 0) return;
    const stat_slice = stat_buf[0..@intCast(rd_rc)];
    const close_paren = std.mem.lastIndexOfScalar(u8, stat_slice, ')') orelse return;
    if (close_paren + 2 > stat_slice.len) return;
    var fields = std.mem.tokenizeScalar(u8, stat_slice[close_paren + 1 ..], ' ');
    _ = fields.next() orelse return; // state
    const ppid_s = fields.next() orelse return; // ppid
    const ppid = std.fmt.parseInt(i32, ppid_s, 10) catch return;
    if (ppid != self_pid) return;
    // Pgrp is the next field (after ppid). A process living in a
    // protected worker's process group is that worker's domain — spared here (the
    // worker's Phase 4 group sweep owns it). Only this read-order survives `comm`
    // values containing spaces: everything after the last ')' is positional.
    const pgrp_s = fields.next() orelse return; // pgrp
    const pgrp = std.fmt.parseInt(i32, pgrp_s, 10) catch return;
    if (pidInList(protected_groups, pgrp)) return;

    // Per-file ownership, starttime guard: a descendant starts strictly
    // after its ancestor, so a candidate whose starttime is >= the oldest live worker's starttime
    // can still be a descendant of a live sibling file — spared. Only candidates strictly older
    // than every live worker are provably leftovers of terminated workers (or of the Zygote /
    // FORK-race), and only those are killed. Everything unparsable is skipped, never killed
    // (skipped, never killed). `older_than_ticks == null` (quiescent sweep, final
    // containment) selects kill-everything-not-protected: no live worker exists to protect.
    if (older_than_ticks) |threshold| {
        const starttime = parseStartTimeTicks(stat_slice) orelse return; // unparsable -> spare
        if (starttime >= threshold) return; // younger-or-equal -> may belong to a live sibling
    }

    // Adopted orphan (FORK-race worker or reparented grandchild): kill its group only
    // when it verified as a group leader, then reap.
    // Status not recorded (stale-pid collisions).
    // getpgid(pid)==pid is mandatory before killGroup — the pgid namespace
    // outlives the group leader's death; on pid reuse kill(-pid) would hit a foreign group.
    const adoptee_pgrp_rc = linux.syscall1(.getpgid, @as(usize, @intCast(pid)));
    if (linux.errno(adoptee_pgrp_rc) == .SUCCESS and adoptee_pgrp_rc == @as(usize, @intCast(pid))) {
        killGroup(pid);
    }
    _ = linux.kill(pid, linux.SIG.KILL);
    // Bounded sweep — after the 10 s budget is spent the kill still
    // fires (containment first) but the reap is skipped so the supervisor cannot stall
    // behind N unkillable D-state adoptees.
    const now_ms = monotonicMs();
    if (now_ms < budget_deadline) {
        const wait_deadline: i64 = @min(budget_deadline, now_ms + 2_000);
        _ = waitPidWithDeadline(pid, wait_deadline, neverInterrupted);
    }
}

/// The orphan sweep must not react to SIGINT/SIGTERM mid-flight: it is the
/// containment path an interrupt triggers, and it is bounded by its own 10 s
/// budget + 2 s per-adoptee reap cap. The isInterrupted hook exists for the
/// interactive worker-reap path (main.zig); here a constant no-op keeps the
/// sweep uninterruptible, matching the Darwin twin's reapChildBounded contract.
fn neverInterrupted() bool {
    return false;
}

/// `starttime` of a process from an already READ /proc/<pid>/stat buffer.
///
/// Field layout (man 5 proc), 1-based: 1 pid, 2 comm, 3 state, 4 ppid, 5 pgrp, 6 session,
/// 7 tty_nr, 8 tpgid, 9 flags, 10 minflt, 11 cminflt, 12 majflt, 13 cmajflt, 14 utime,
/// 15 stime, 16 cutime, 17 cstime, 18 priority, 19 nice, 20 num_threads, 21 itrealvalue,
/// 22 starttime. Fields 3.. are positional only after the last ')' (comm may contain spaces and
/// ')'), so the first token after the last ')' is field 3 (state) and starttime — field 22 —
/// is token index 22 - 3 = 19. Nineteen tokens are therefore skipped before the read.
pub fn parseStartTimeTicks(stat_slice: []const u8) ?u64 {
    // Truncation guard: /proc/<pid>/stat lines always end with '\n'. A buffer that does not is
    // cut short (the caller reads into a fixed 512-byte buffer), and a truncated number parses
    // into a smaller value — i.e. a candidate would look older than it is and could be killed.
    // "Cannot tell" must spare, never kill.
    if (stat_slice.len == 0 or stat_slice[stat_slice.len - 1] != '\n') return null;
    const close_paren = std.mem.lastIndexOfScalar(u8, stat_slice, ')') orelse return null;
    if (close_paren + 2 > stat_slice.len) return null;
    var fields = std.mem.tokenizeScalar(u8, stat_slice[close_paren + 1 ..], ' ');
    var skipped: usize = 0;
    while (skipped < 19) : (skipped += 1) _ = fields.next() orelse return null;
    const starttime_s = fields.next() orelse return null;
    return std.fmt.parseInt(u64, starttime_s, 10) catch null;
}

/// Starttime (field 22, see `parseStartTimeTicks`) of `pid`, read from /proc. `null` when
/// the process is gone (ENOENT), unreadable or the line is malformed — callers must treat null as
/// "unknown" and fail closed (never kill on unknown ownership).
pub fn procStartTimeTicks(pid: i32) ?u64 {
    if (pid <= 0) return null;
    var path_buf: [64]u8 = @splat(0);
    const path = std.fmt.bufPrintSentinel(&path_buf, "/proc/{d}/stat", .{pid}, 0) catch return null;
    const fd_rc = linux.open(path.ptr, .{ .ACCMODE = .RDONLY, .CLOEXEC = true }, 0);
    if (linux.errno(fd_rc) != .SUCCESS) return null;
    const fd: fd_t = @intCast(fd_rc);
    var stat_buf: [512]u8 = @splat(0);
    const rd_rc = linux.read(fd, &stat_buf, stat_buf.len);
    _ = linux.close(fd);
    if (linux.errno(rd_rc) != .SUCCESS or rd_rc <= 0) return null;
    return parseStartTimeTicks(stat_buf[0..@intCast(rd_rc)]);
}

/// FAIL-CLOSED group kill: kill(-pgid) with pgid == 1 is kill(-1) — a broadcast
/// SIGKILL to the whole uid; pgid == 0 is kill(0) — our own group. Neither form
/// is ever a legal containment target of ztest (a worker group is always
/// pgid == pid > 1). The refusal is silent and cheap: an ESRCH equivalent.
pub fn killGroup(pgid: i32) void {
    if (pgid <= 1) return;
    _ = linux.kill(-pgid, linux.SIG.KILL);
}

/// Bounded WNOHANG reap loop: never blocks forever on a D-state worker.
/// Returns the raw wait status, or -1 if the child could not be reaped within `deadline_ms`.
pub fn reapChildBounded(pid: i32, deadline_ms: i64) i32 {
    var status: i32 = -1;
    while (true) {
        const wrc = linux.wait4(pid, &status, linux.W.NOHANG, null);
        switch (linux.errno(wrc)) {
            .SUCCESS => {
                if (wrc != 0) return status; // reaped
            },
            .INTR => continue,
            else => return -1, // ECHILD: not our child (already reaped elsewhere)
        }
        if (deadline_ms - monotonicMs() <= 0) return -1;
        var req_ts: linux.timespec = .{ .sec = 0, .nsec = 2_000_000 };
        _ = linux.nanosleep(&req_ts, null);
    }
}

/// Non-blocking wait4 loop with deadline + WNOWAIT group sweep.
/// Interrupt-aware: on SIGINT/SIGTERM (reported by the caller's `isInterrupted` hook) it SIGKILLs
/// the worker group and reaps with a bound instead of waiting out the full deadline.
pub fn waitPidWithDeadline(pid: i32, deadline_ms: i64, isInterrupted: fn () bool) i32 {
    var siginfo_buf: [128]u8 align(8) = @splat(0);
    while (deadline_ms - monotonicMs() > 0) {
        if (isInterrupted()) {
            killGroup(pid);
            _ = linux.kill(pid, linux.SIG.KILL);
            return reapChildBounded(pid, monotonicMs() + 2_000);
        }
        // Peek with WEXITED | WNOHANG | WNOWAIT so pid stays pinned as zombie while we sweep -pid.
        const wrc = linux.syscall5(
            .waitid,
            1, // P_PID
            @as(usize, @intCast(pid)),
            @intFromPtr(&siginfo_buf),
            linux.W.EXITED | linux.W.NOHANG | linux.W.NOWAIT,
            0,
        );
        if (linux.errno(wrc) == .INTR) continue;
        if (linux.errno(wrc) != .SUCCESS) return -1; // ECHILD: not our child — never kill a pid we do not own
        const si_pid = siginfoPid(&siginfo_buf);
        if (si_pid == pid) {
            // Leader is now a zombie holding PGID `pid`: safe to SIGKILL any remaining grandchildren in `-pid`.
            killGroup(pid);
            return reapChildBounded(pid, monotonicMs() + 2_000);
        }
        var req_ts: linux.timespec = .{ .sec = 0, .nsec = 2_000_000 };
        _ = linux.nanosleep(&req_ts, null);
    }
    killGroup(pid);
    _ = linux.kill(pid, linux.SIG.KILL);
    return reapChildBounded(pid, monotonicMs() + 2_000);
}

/// Watchdog registry (ADR-004 layer 3): capacity — 2× MAX_SLOTS (16 worker slots => 32 registry
/// slots, ≥ 2× the maximum parallelism required by ADR-004; headroom for all simultaneous registrations).
pub const PgidRegistry = struct {
    pub const CAPACITY: usize = 32;
    pub const MAP_LEN: usize = CAPACITY * 8; // 256 bytes: one page-aligned map

    pub const View = struct {
        slots: []align(8) volatile u64,

        /// Atomic (aligned u64 store on x86_64) registration of a live worker's pgid.
        /// pgid > 1 is valid (pgid = pid > 1 after setpgid(0,0) => pgid == the worker's pid),
        /// 0 — "slot free". pgid <= 1 is rejected: pgid == 1 under kill(-pgid) is
        /// kill(-1) — a broadcast SIGKILL to the whole uid; pgid == 0 is kill(0) — our
        /// own group. Neither form is a legal target.
        pub fn register(self: View, pgid: i32) bool {
            if (pgid <= 1) return false;
            const v: u64 = @intCast(pgid);
            // Idempotency: registration may happen before accept
            // (FORKED-parse / post-setpgid) and at accept — re-registering the same pgid must not
            // take a second slot (else killAll would fire twice and unregister
            // would free only the first duplicate).
            for (self.slots) |*s| {
                if (@atomicLoad(u64, s, .monotonic) == v) return true;
            }
            for (self.slots) |*s| {
                if (@atomicLoad(u64, s, .monotonic) == 0) {
                    @atomicStore(u64, s, v, .release);
                    return true;
                }
            }
            return false; // registry overflow — registry fail-open (see the comment below)
        }

        /// Frees the slot on the worker's REAP (slot reuse).
        pub fn unregister(self: View, pgid: i32) void {
            if (pgid <= 1) return;
            const v: u64 = @intCast(pgid);
            for (self.slots) |*s| {
                if (@atomicLoad(u64, s, .monotonic) == v) {
                    @atomicStore(u64, s, 0, .release);
                    return;
                }
            }
        }

        /// Watchdog: walks the registry after the writer's death, killpg on every nonzero entry.
        pub fn killAll(self: View) void {
            for (self.slots) |*s| {
                const v = @atomicLoad(u64, s, .acquire);
                if (v == 0) continue;
                const pgid: i32 = @intCast(v);
                killGroup(pgid);
            }
        }
    };

    /// Allocates the registry's anonymous shared mapping (MAP_SHARED|MAP_ANONYMOUS), zeroed.
    pub fn create() !View {
        const rc = linux.mmap(
            null,
            MAP_LEN,
            .{ .READ = true, .WRITE = true },
            .{ .TYPE = .SHARED, .ANONYMOUS = true },
            -1,
            0,
        );
        if (linux.errno(rc) != .SUCCESS) return error.WatchdogMmapFailed;
        const addr: [*]align(8) u8 = @ptrFromInt(rc);
        return .{ .slots = @as([*]volatile u64, @ptrCast(addr))[0..CAPACITY] };
    }

    pub fn destroy(view: View) void {
        _ = linux.munmap(@ptrCast(@volatileCast(view.slots.ptr)), MAP_LEN);
    }
};

/// Run watchdog (ADR-004 layer 3): One fork for the whole run, one process.
/// The watchdog is a direct child of the supervisor placed in its own group:
///  - setpgid(0,0) right after fork — a killpg from a test at a worker group never touches it,
///    and it is not a member of the supervisor's group;
///  - close_range(3..∞) — only 0/1/2 and the supervisor's pidfd remain;
///  - poll(pidfd) blocks until the supervisor dies (a pidfd stays valid in the zombie state);
///  - on the writer's death: killAll(reg_view) — killpg(-pgid, SIGKILL) per entry — _exit(0).
/// Test code cannot reach it: it is not a child of a worker (os.wait(-1) in tests never lands
/// on it), it is not python (survives `pkill python`), and it is in no worker group.
/// The supervisor reaps it by pid on a normal run end (the defer in runMain: kill+reap).
pub fn armPdeathsigOn(parent_pid: i32) void {
    _ = linux.prctl(@backingInt(linux.PR.SET_PDEATHSIG), @backingInt(linux.SIG.KILL), 0, 0, 0);
    if (@as(i32, @intCast(linux.getppid())) != parent_pid) {
        linux.exit_group(125);
    }
}

/// Pdeathsig cascade (ADR-004 layer 1, pre-exec arm): called in the child branch of the supervisor's
/// fork before execve. prctl survives execve (except setugid), and is reset in the child after fork.
/// The getppid check right after arming: a parent death that already happened does not
/// retroactively deliver the signal; the immediate getppid check closes that race.
pub fn armPdeathsigPreExec(parent_pid: i32) void {
    armPdeathsigOn(parent_pid);
}

/// Spawns the run watchdog for the supervisor: a fork whose child polls the supervisor's
/// death-source fd forever and, when it fires, walks the shared pgid registry with killpg.
/// `reg_view` must be the live PgidRegistry view of the caller (shared MAP_SHARED mapping —
/// the child refers to the same pages as the supervisor).
/// Returns the watchdog pid, or null on degradation (containment then rests on layers 1+2).
pub fn spawnBatchWatchdog(reg_view: PgidRegistry.View) ?i32 {
    const sup_pidfd = openSupervisorDeathFd(@intCast(linux.getpid()));
    if (sup_pidfd < 0) return null; // degraded: no watchdog, containment by layers 1+2 only
    const wpid_rc = linux.fork();
    if (linux.errno(wpid_rc) != .SUCCESS) {
        _ = linux.close(sup_pidfd);
        return null; // degraded
    }
    if (wpid_rc != 0) {
        // Supervisor: the pidfd is needed only by the watchdog; the watchdog is not a member of the supervisor's
        // group nor of any worker group; no test waitpids it (not their child); its zombie, if it
        // died, is acceptable — the runMain defer reaps it on the normal path anyway.
        _ = linux.close(sup_pidfd);
        return @intCast(wpid_rc);
    }
    // ---- watchdog (the single process; a child of the supervisor) ----
    // Own group: not in any worker group and not in the supervisor's group.
    _ = linux.setpgid(0, 0);
    // Close everything except 0/1/2 and the supervisor's pidfd. The two ranges — [sup_pidfd+1, ∞)
    // and [3, sup_pidfd) — keep the pidfd itself alive (close_range is inclusive
    // only within the given range). The watchdog polls this fd for its whole life.
    closeAllFdsExcept(sup_pidfd);
    var pfd: [1]linux.pollfd = .{.{
        .fd = sup_pidfd,
        .events = linux.POLL.IN,
        .revents = 0,
    }};
    while (true) {
        const prc = linux.poll(&pfd, 1, -1);
        switch (linux.errno(prc)) {
            .SUCCESS => {},
            .INTR => continue,
            else => break, // EBADF etc.: a non-blocking exit without cleanup
        }
        if (prc != 0) break; // pidfd readable => the supervisor is dead
    }
    // The supervisor is dead: the reader walks the registry, the writer is gone.
    // This view is the shared MAP_SHARED|MAP_ANONYMOUS mapping; its pages are shared with the supervisor.
    reg_view.killAll();
    linux.exit_group(0);
}

pub const PeerCredError = error{ PeerCredFailed, UnauthorizedPeer, WorkerPidMismatch };

pub const Ucred = extern struct {
    pid: i32,
    uid: u32,
    gid: u32,
};

/// Verifies peer credentials (`SO_PEERCRED`) of an accepted Unix domain socket connection.
pub fn verifySocketPeerCred(conn_fd: fd_t, expected_uid: u32, expected_pid: ?i32) PeerCredError!Ucred {
    var cred: Ucred = .{ .pid = -1, .uid = 0xFFFFFFFF, .gid = 0xFFFFFFFF };
    var opt_len: u32 = @sizeOf(Ucred);
    const rc = linux.getsockopt(conn_fd, linux.SOL.SOCKET, linux.SO.PEERCRED, @ptrCast(&cred), &opt_len);
    if (linux.errno(rc) != .SUCCESS or opt_len != @sizeOf(Ucred)) {
        return error.PeerCredFailed;
    }
    if (cred.uid != expected_uid) {
        return error.UnauthorizedPeer;
    }
    if (expected_pid) |epid| {
        if (cred.pid != epid) {
            return error.WorkerPidMismatch;
        }
    }
    return cred;
}

/// Opens a private nonblocking open file description for the supervisor's own copy of fd 1/2 via
/// /proc/self/fd: O_NONBLOCK lives on the OFD, so a worker that inherited fd 1/2 and
/// calls os.set_blocking(1, true) cannot flip the supervisor's writes back to blocking and freeze
/// supervision on a backpressured pipe. A private OFD (CLOEXEC, never inherited) keeps supervisor
/// output flags untouchable by workers. The caller's status flags O_APPEND / O_DSYNC / O_SYNC are
/// carried over from the original OFD (residual). Returns null when the source
/// flags cannot be read or the sink cannot be reopened (caller then degrades to a plain write on the
/// original fd). NOTE: only valid for FIFO and character-device sinks — /proc/self/fd refuses
/// to reopen sockets (ENXIO), and a private OFD on a regular file would get its own file offset and
/// clobber concurrent writers; the caller performs that classification via fstat first.
pub fn openPrivateOutputFd(std_fd: i32) ?fd_t {
    const fl_rc = linux.fcntl(std_fd, linux.F.GETFL, 0);
    if (linux.errno(fl_rc) != .SUCCESS) return null;
    const src_fl: linux.O = @bitCast(@as(u32, @intCast(fl_rc)));
    var path_buf: [32]u8 = undefined;
    const path = std.fmt.bufPrintSentinel(&path_buf, "/proc/self/fd/{d}", .{std_fd}, 0) catch return null;
    const rc = linux.open(path, .{
        .ACCMODE = .WRONLY,
        .NONBLOCK = true,
        .CLOEXEC = true,
        .APPEND = src_fl.APPEND,
        .DSYNC = src_fl.DSYNC,
        .SYNC = src_fl.SYNC,
    }, 0);
    if (linux.errno(rc) != .SUCCESS) return null;
    return @intCast(rc);
}

/// Architecture-independent statx(2) size (works identically on x86_64 and aarch64 Linux).
/// Returns file size in bytes, or 0 on failure (caller degrades: size-0 sorts last in LPT).
pub fn statFileSize(path_z: [*:0]const u8) u64 {
    var stx: linux.Statx = undefined;
    const rc = linux.statx(linux.AT.FDCWD, path_z, 0, .{ .SIZE = true }, &stx);
    if (linux.errno(rc) != .SUCCESS) return 0;
    return stx.size;
}

/// Architecture-independent statx(2) on an open fd.
/// Returns the stx_mode bits, or null on failure (caller degrades conservatively).
pub fn fstatMode(fd: fd_t) ?u32 {
    var stx: linux.Statx = undefined;
    const rc = linux.statx(fd, "", linux.AT.EMPTY_PATH, .{ .TYPE = true, .MODE = true }, &stx);
    if (linux.errno(rc) != .SUCCESS) return null;
    return stx.mode;
}

/// statx(2) mode bits for a path, or null when the path is absent or cannot be stat'ed.
/// Symlinks are followed, which is the existence test a runner must apply to its arguments.
pub fn statModeBits(path: []const u8) ?u32 {
    var buf: [4096]u8 = undefined;
    const pz = std.fmt.bufPrintSentinel(&buf, "{s}", .{path}, 0) catch return null;
    var stx: linux.Statx = undefined;
    const rc = linux.statx(linux.AT.FDCWD, pz.ptr, 0, .{ .TYPE = true, .MODE = true }, &stx);
    if (linux.errno(rc) != .SUCCESS) return null;
    return stx.mode;
}

/// Canonicalize an existing path without changing the established Linux
/// O_PATH/readlinkat(AT_EMPTY_PATH) behavior. The /proc fallback is retained
/// for filesystems/kernels where the empty-path operation is unavailable.
pub fn canonicalizePath(path_z: [*:0]const u8, out: [*]u8) ?usize {
    const open_rc = linux.openat(linux.AT.FDCWD, path_z, .{ .PATH = true, .CLOEXEC = true }, 0);
    if (linux.errno(open_rc) != .SUCCESS) return null;
    const fd: fd_t = @intCast(open_rc);
    defer _ = linux.close(fd);
    const rc = linux.readlinkat(fd, "", out, 4096);
    if (linux.errno(rc) == .SUCCESS and rc > 0) return @intCast(rc);
    var proc_buf: [64]u8 = @splat(0);
    const proc_z = std.fmt.bufPrintSentinel(&proc_buf, "/proc/self/fd/{d}", .{fd}, 0) catch return null;
    const fallback = linux.readlinkat(linux.AT.FDCWD, proc_z.ptr, out, 4096);
    if (linux.errno(fallback) != .SUCCESS or fallback == 0) return null;
    return @intCast(fallback);
}

/// Directory identity for the discovery visited-set: the key is taken from
/// fstat of the already-open directory fd, i.e. after the kernel resolved any symlink chain on
/// the path. Two different symlink paths into the same directory collapse to one (st_dev, st_ino)
/// key, so directory cycles (a→., b→.) are pruned on entry instead of recursing exponentially.
pub const DirKey = struct { dev: u64, ino: u64 };

/// statx(2) on an open fd; null on syscall failure, letting discovery degrade to
/// untracked recursion (an outcome statx on an open directory fd practically never produces).
pub fn dirKeyOfFd(fd: fd_t) ?DirKey {
    var stx: linux.Statx = undefined;
    const rc = linux.statx(fd, "", linux.AT.EMPTY_PATH, .{ .INO = true }, &stx);
    if (linux.errno(rc) != .SUCCESS) return null;
    const dev: u64 = (@as(u64, stx.dev_major) << 32) | @as(u64, stx.dev_minor);
    return .{ .dev = dev, .ino = stx.ino };
}

pub fn isDirectoryProbe(path_slice: []const u8) bool {
    var pbuf: [4096]u8 = @splat(0);
    const pz = std.fmt.bufPrintSentinel(&pbuf, "{s}", .{path_slice}, 0) catch return false;
    const rc = linux.open(pz.ptr, .{ .ACCMODE = .RDONLY, .DIRECTORY = true, .CLOEXEC = true }, 0);
    if (linux.errno(rc) == .SUCCESS) {
        _ = linux.close(@intCast(rc));
        return true;
    }
    return false;
}

pub fn isReg(mode_bits: u32) bool {
    return linux.S.ISREG(mode_bits);
}

pub fn isFifo(mode_bits: u32) bool {
    return linux.S.ISFIFO(mode_bits);
}

pub fn isChr(mode_bits: u32) bool {
    return linux.S.ISCHR(mode_bits);
}

pub fn isBlk(mode_bits: u32) bool {
    return linux.S.ISBLK(mode_bits);
}

pub fn isSock(mode_bits: u32) bool {
    return linux.S.ISSOCK(mode_bits);
}

/// Raw O_NONBLOCK status bit (packed-struct form is awkward for masking in tests).
pub const O_NONBLOCK_BIT: u32 = @as(u32, @bitCast(linux.O{ .NONBLOCK = true }));

pub const MSG_DONTWAIT: u32 = 0x40; // Linux x86-64: per-call non-blocking send
pub const MSG_NBIO: u32 = 0; // Darwin-only sbwait gate; Linux MSG_DONTWAIT covers both paths
pub const MSG_NOSIGNAL: u32 = 0x4000; // Linux x86-64: no SIGPIPE on dead peer

/// Reads ru_maxrss of a reaped child via wait4-style usage. Linux reports KiB; this PAL
/// function normalizes to bytes so the supervisor prints one unit on every OS.
/// Callers pass the rusage filled by their own wait4 call (Zig's linux.wait4 does not
/// expose rusage, so this is filled by a dedicated wait4 variant in the future phase —
/// currently a documented placeholder returning 0 when unavailable).
pub fn readMaxRssBytes(ru_maxrss_kib: i64) i64 {
    // Linux: ru_maxrss is KiB (getrusage(2)).
    return ru_maxrss_kib * 1024;
}
