//! Darwin (macOS) backend of the ztest platform abstraction layer.
//!
//! Parity strategy: the ADR-004 containment contract is preserved mechanism-for-mechanism:
//!   * pidfd_open/poll(POLLIN)                -> kqueue(2) + EVFILT_PROC / NOTE_EXIT (+NOTE_TRACK
//!                                               where the kernel supports it, closing the PID-reuse race);
//!   * PR_SET_PDEATHSIG                       -> a kqueue parent-death watcher armed pre-exec
//!                                               (self-kill on parent NOTE_EXIT, plus the same
//!                                               getppid() verification right after arming);
//!   * PR_SET_CHILD_SUBREAPER                 -> no-op: Darwin has no subreaper; containment of
//!                                               orphaned descendants is carried by the group kills
//!                                               and the libproc sweep below;
//!   * SO_PEERCRED                            -> getsockopt(SOL_LOCAL, LOCAL_PEERCRED, xucred)
//!                                               + getsockopt(SOL_LOCAL, LOCAL_PEERPID);
//!   * /proc/self/task/<pid>/children + stat  -> libproc: proc_listchildpids + proc_pidinfo
//!                                               (PROC_PIDTBSDINFO: start time, pgid);
//!   * /proc/self/fd/1 reopen                 -> F_GETPATH + fresh open (new OFD, inode-checked).
//!
//! All entry points are externs into libSystem (no frameworks). This backend therefore REQUIRES
//! libc linking: build.zig must set `.link_libc = true` for Darwin targets (orchestrator note).
//! Constants follow XNU public headers: bsd/sys/event.h (kqueue/EVFILT_PROC), bsd/sys/ucred.h
//! (xucred), bsd/sys/proc_info.h (proc_bsdinfo / PROC_PIDTBSDINFO), libproc.h (proc_listchildpids,
//! proc_pidinfo).

const std = @import("std");
const builtin = @import("builtin");

comptime {
    if (builtin.os.tag != .macos) {
        @compileError("pal/darwin.zig must only be imported when targeting macOS (use pal.mod)");
    }
}

pub const fd_t = i32;

/// Neutral discovery record (linux dirent64 wire layout) parsed by the shared
/// discovery loop in main.zig on every OS. On Darwin the writer is
/// posix_darwin.getdents64, which converts native getdirentries records; the
/// layout is defined once in posix_darwin.zig and re-exported here so the
/// writer and the parser can never drift apart.
pub const LinuxDirent64 = @import("posix_darwin.zig").LinuxDirent64;

// ----------------------------------------------------------------------------
// libSystem externs
// ----------------------------------------------------------------------------
extern "c" fn kqueue() i32;
extern "c" fn kevent(kq: i32, changelist: ?[*]const Kevent, nchanges: i32, eventlist: ?[*]Kevent, nevents: i32, timeout: ?*const Timespec) i32;
extern "c" fn nanosleep(req: *const Timespec, rem: ?*Timespec) i32;
extern "c" fn fork() i32;
extern "c" fn getpid() i32;
extern "c" fn getppid() i32;
extern "c" fn getuid() u32;
extern "c" fn getpgid(pid: i32) i32;
extern "c" fn setpgid(pid: i32, pgid: i32) i32;
extern "c" fn kill(pid: i32, sig: i32) i32;
extern "c" fn waitpid(pid: i32, status: ?*i32, options: i32) i32;
extern "c" fn waitid(idtype: i32, id: u32, infop: ?*SiginfoDarwin, options: i32) i32;
extern "c" fn getsockopt(s: i32, level: i32, name: i32, value: ?*anyopaque, value_len: *u32) i32;
extern "c" fn fcntl(fd: i32, cmd: i32, ...) i32;
extern "c" fn open(path: [*:0]const u8, flags: i32, ...) i32;
extern "c" fn close(fd: i32) i32;
extern "c" fn _exit(status: i32) noreturn;
extern "c" fn proc_listchildpids(ppid: i32, buffer: ?*anyopaque, buffersize: i32) i32;
extern "c" fn proc_pidinfo(pid: i32, flavor: i32, arg: u64, buffer: ?*anyopaque, buffersize: i32) i32;
extern "c" fn gettimeofday(tp: ?*Timeval, tzp: ?*anyopaque) i32;
extern "c" fn mach_absolute_time() u64;
extern "c" fn realpath(path: [*:0]const u8, resolved: [*]u8) ?[*]u8;
extern "c" fn strlen(s: [*]const u8) usize;

// ----------------------------------------------------------------------------
// XNU constants (bsd/sys/event.h, bsd/sys/wait.h, bsd/sys/socket.h, libproc.h)
// ----------------------------------------------------------------------------
pub const EVFILT_PROC: i32 = -5;
pub const NOTE_EXIT: u32 = 0x80000000;
pub const NOTE_FORK: u32 = 0x40000000;
pub const NOTE_TRACK: u32 = 0x00000002; // EVFLAG1-style child tracking for NOTE_FORK
pub const NOTE_EXITSTATUS: u32 = 0x04000000;
pub const EV_ADD: u16 = 0x0001;
pub const EV_ENABLE: u16 = 0x0004;
pub const EV_CLEAR: u16 = 0x0020;
pub const EV_ONESHOT: u16 = 0x0010;

pub const WNOHANG: i32 = 0x00000001;
pub const WEXITED_WAITID: i32 = 0x00000004; // waitid(2) WEXITED on Darwin
pub const WNOWAIT: i32 = 0x00000020;
pub const P_ALL: i32 = 0;
pub const P_PID: i32 = 1;

pub const SOL_LOCAL: i32 = 0; // Darwin: local-domain socket options level
pub const LOCAL_PEERCRED: i32 = 0x001;
pub const LOCAL_PEERPID: i32 = 0x002;

pub const PROC_PIDTBSDINFO: i32 = 3;
pub const PROC_PIDTASKINFO: i32 = 4;

pub const SIGKILL: i32 = 9;
pub const POLLIN: i16 = 0x0001;
pub const O_WRONLY: i32 = 0x0001;
pub const O_NONBLOCK: i32 = 0x0004;
pub const O_APPEND: i32 = 0x0008;
pub const O_DSYNC: i32 = 0x400000;
pub const O_SYNC: i32 = 0x0080;
pub const O_CLOEXEC: i32 = 0x01000000;
pub const F_GETFL: i32 = 3;
pub const F_GETPATH: i32 = 50;
pub const MAXPATHLEN: usize = 1024;

/// struct kevent (bsd/sys/event.h): 64-bit layout on all supported Darwin ABIs.
pub const Kevent = extern struct {
    ident: usize,
    filter: i16,
    flags: u16,
    fflags: u32,
    data: isize,
    udata: ?*anyopaque,
};

pub const Timespec = extern struct { sec: isize, nsec: isize };
pub const Timeval = extern struct { sec: isize, usec: i32 };

/// struct xucred (bsd/sys/ucred.h): returned by LOCAL_PEERCRED.
pub const Xucred = extern struct {
    xu_version: u32, // XUCRED_VERSION
    xu_uid: u32,
    xu_ngroups: i16,
    xu_groups: [16]u32,
};

/// Darwin siginfo_t (bsd/sys/signal.h): si_pid is a typed field here — never a raw offset read
/// like on Linux (the layouts differ; the Linux 16-byte shortcut would be wrong on Darwin).
pub const SiginfoDarwin = extern struct {
    si_signo: i32,
    si_errno: i32,
    si_code: i32,
    si_pid: i32,
    si_uid: u32,
    si_status: i32,
    si_addr: usize,
    si_value: extern union { sival_int: i32, sival_ptr: usize },
    si_band: c_long,
    __pad: [7]c_ulong,
};

/// struct proc_bsdinfo (bsd/sys/proc_info.h), 64-bit layout. Field order is ABI-frozen by XNU;
/// pbi_start_tvsec (start time, seconds) and pbi_pgid are the two fields the PAL consumes.
/// Source: public XNU bsd/sys/proc_info.h; MAXCOMLEN is 16. Keep the complete
/// prefix explicitly instead of relying on a C compiler's inferred padding.
pub const MAXCOMLEN: usize = 16;
pub const ProcBsdInfo = extern struct {
    pbi_flags: u32,
    pbi_status: u32,
    pbi_xstatus: u32,
    pbi_pid: u32,
    pbi_ppid: u32,
    pbi_uid: u32,
    pbi_gid: u32,
    pbi_ruid: u32,
    pbi_rgid: u32,
    pbi_svuid: u32,
    pbi_svgid: u32,
    rfu_1: u32,
    pbi_comm: [MAXCOMLEN]u8,
    pbi_name: [2 * MAXCOMLEN]u8,
    pbi_nfiles: u32,
    pbi_pgid: u32,
    pbi_pjobc: u32,
    e_tdev: u32,
    e_tpgid: u32,
    pbi_nice: i32,
    pbi_start_tvsec: u64,
    pbi_start_tvusec: u64,
};

comptime {
    // The fields the PAL reads must sit where the XNU header puts them; a layout drift
    // here would silently read a foreign process's pgid/start time. Fail at compile time.
    // Offsets from bsd/sys/proc_info.h (12 x u32 prefix = 48, + comm[16] = 64,
    // + name[2*MAXCOMLEN=32] = 96, nfiles@96, pgid@100, pjobc/e_tdev/e_tpgid/nice
    // 104..119, start_tvsec@120 aligned to 8, tvusec@128, sizeof = 136).
    if (@offsetOf(ProcBsdInfo, "pbi_pgid") != 100) @compileError("proc_bsdinfo.pbi_pgid offset drifted");
    if (@offsetOf(ProcBsdInfo, "pbi_start_tvsec") != 120) @compileError("proc_bsdinfo.pbi_start_tvsec offset drifted");
    if (@sizeOf(ProcBsdInfo) != 136) @compileError("proc_bsdinfo size drifted");
}

// ----------------------------------------------------------------------------
// Clock
// ----------------------------------------------------------------------------
var g_mach_timebase_num: u32 = 0;
var g_mach_timebase_den: u32 = 0;

extern "c" fn mach_timebase_info(info: *MachTimebaseInfo) i32;
const MachTimebaseInfo = extern struct { numer: u32, denom: u32 };

/// Monotonic milliseconds via mach_absolute_time (the Darwin equivalent of CLOCK_MONOTONIC).
/// The timebase is cached after first use; numer/denom are 1/1 on every macOS since 10.12,
/// but the conversion is done properly for older hardware.
pub fn monotonicMs() i64 {
    if (g_mach_timebase_den == 0) {
        var info: MachTimebaseInfo = .{ .numer = 1, .denom = 1 };
        _ = mach_timebase_info(&info);
        g_mach_timebase_num = info.numer;
        g_mach_timebase_den = info.denom;
    }
    const abs_ns = mach_absolute_time() * g_mach_timebase_num / g_mach_timebase_den;
    return @intCast(abs_ns / 1_000_000);
}

// ----------------------------------------------------------------------------

/// PATH_MAX-sized output buffer, matching the shared discovery buffer.
pub fn canonicalizePath(path_z: [*:0]const u8, out: [*]u8) ?usize {
    const resolved = realpath(path_z, out) orelse return null;
    return strlen(resolved);
}

// Process containment primitives
// ----------------------------------------------------------------------------

/// Darwin has no PR_SET_CHILD_SUBREAPER: orphaned descendants reparent to launchd, not to us.
/// Containment of escapees is carried by (a) process-group kills of the worker leader and
/// (b) the libproc children sweep in killAdoptedOrphans. Kept as a no-op so the supervisor's
/// startup sequence is OS-neutral.
pub fn enableChildSubreaper() void {}

pub const PgidCheck = enum { leader, dead_or_reaped, violator };

/// Verifies the process-group invariant `getpgid(pid) == pid` (same contract as Linux).
pub fn checkPeerPgid(pid: i32) PgidCheck {
    if (pid <= 0) return .violator;
    const pg = getpgid(pid);
    if (pg == pid) return .leader;
    // On XNU, getpgid() uses proc_find() which skips SZOMB (zombies return -1/ESRCH),
    // whereas kill(pid, 0) returns 0 on zombies. Match Linux PAL (src/pal/linux.zig:66):
    // negative getpgid on pid > 0 means dead/zombie/reaped.
    if (pg < 0) return .dead_or_reaped;
    _ = setpgid(pid, pid); // best-effort retry (legal only pre-exec)
    const pg2 = getpgid(pid);
    if (pg2 == pid) return .leader;
    if (pg2 < 0) return .dead_or_reaped;
    return .violator;
}

/// pidfd equivalent: a kqueue descriptor holding EVFILT_PROC/NOTE_EXIT on `pid`.
/// poll(POLLIN) on it fires when the process exits — the dispatcher's poll loop is unchanged.
/// The process watch pins the exit notification for this kernel object; it is
/// not a claim that numeric pid reuse can be detected after an unrelated reap.
/// Returns -1 on any failure (caller degrades to the deadline-only behavior, as on Linux).
pub fn pidfdOpen(pid: i32) fd_t {
    return @import("posix_darwin.zig").pidfdOpen(pid);
}

/// Watchdog death-source fd: kqueue watch on the supervisor's pid (NOTE_EXIT).
pub fn openSupervisorDeathFd(pid: i32) fd_t {
    return pidfdOpen(pid);
}

/// Darwin has no close_range(2); the watchdog closes 3..255 explicitly (the supervisor never
/// holds more; the kqueue death fd is the single keeper). Bounded loop, no syscall per fd
/// beyond the closes themselves.
pub fn closeAllFdsExcept(keep_fd: fd_t) void {
    var fd: i32 = 3;
    while (fd < 256) : (fd += 1) {
        if (fd != keep_fd) _ = close(fd);
    }
}

/// Selectively reaps zombie children without recording statuses (same contract as Linux).
/// `protected_wd_pid` spares the run watchdog; the seen-set breaks the WNOWAIT peek cycle.
pub fn reapAdoptedChildren(protected: []const i32, protected_wd_pid: i32) void {
    var seen_buf: [256]i32 = undefined;
    var seen_n: usize = 0;
    while (true) {
        var si: SiginfoDarwin = std.mem.zeroes(SiginfoDarwin);
        const rc = waitid(P_ALL, 0, &si, WEXITED_WAITID | WNOHANG | WNOWAIT);
        if (rc != 0) return; // ECHILD: no children at all
        const pid = si.si_pid;
        if (pid <= 0) return;
        if (pid == protected_wd_pid or pidInList(protected, pid)) {
            if (pidInList(seen_buf[0..seen_n], pid)) return;
            if (seen_n >= seen_buf.len) return;
            seen_buf[seen_n] = pid;
            seen_n += 1;
            continue;
        }
        var st: i32 = -1;
        while (true) {
            const wrc = waitpid(pid, &st, 0);
            if (wrc == pid) break;
            if (wrc < 0) {
                st = -1; // ECHILD race: already reaped elsewhere
                break;
            }
        }
        seen_n = 0;
    }
}

pub fn pidInList(list: []const i32, pid: i32) bool {
    for (list) |p| {
        if (p == pid) return true;
    }
    return false;
}

/// Thread count of a process, from proc_taskinfo.pti_threadnum. A hard-coded
/// single-thread answer would silently disable the zygote fork-safety guard as
/// soon as Python or a third-party module created a thread.
pub fn countProcTasks(pid: i32) !usize {
    if (pid <= 0) return error.ProcTaskOpenFailed;
    const ProcTaskInfo = extern struct {
        virtual_size: u64,
        resident_size: u64,
        total_user: u64,
        total_system: u64,
        threads_user: u64,
        threads_system: u64,
        policy: i32,
        faults: i32,
        pageins: i32,
        cow_faults: i32,
        messages_sent: i32,
        messages_received: i32,
        syscalls_mach: i32,
        syscalls_unix: i32,
        csw: i32,
        threadnum: i32,
        numrunning: i32,
        priority: i32,
    };
    comptime {
        if (@offsetOf(ProcTaskInfo, "threadnum") != 84) @compileError("proc_taskinfo.pti_threadnum offset drifted");
        if (@sizeOf(ProcTaskInfo) != 96) @compileError("proc_taskinfo size drifted");
    }
    var info: ProcTaskInfo = std.mem.zeroes(ProcTaskInfo);
    const rc = proc_pidinfo(pid, PROC_PIDTASKINFO, 0, &info, @sizeOf(ProcTaskInfo));
    if (rc != @sizeOf(ProcTaskInfo) or info.threadnum < 1) return error.ProcTaskOpenFailed;
    return @intCast(info.threadnum);
}

/// Start time of `pid` in SECONDS (proc_bsdinfo.pbi_start_tvsec), or null when unknown.
/// Callers compare start times only against other values from the same function, so the
/// unit (seconds vs Linux jiffies) never crosses the PAL boundary; strict ordering is all
/// the ownership discriminator needs. Unknown -> spare (fail closed), as on Linux.
pub fn procStartTimeTicks(pid: i32) ?u64 {
    if (pid <= 0) return null;
    var info: ProcBsdInfo = std.mem.zeroes(ProcBsdInfo);
    const rc = proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, @sizeOf(ProcBsdInfo));
    if (rc != @sizeOf(ProcBsdInfo)) return null;
    if (info.pbi_start_tvsec == 0) return null;
    return info.pbi_start_tvsec;
}

/// FAIL-CLOSED group kill (identical contract to the Linux backend): pgid <= 1 is never a
/// legal killpg target.
pub fn killGroup(pgid: i32) void {
    if (pgid <= 1) return;
    _ = kill(-pgid, SIGKILL);
}

/// Scans this process's children (libproc proc_listchildpids) and kills+reaps every one not
/// in `exclude` / not in a protected group. Same filter contract as the Linux /proc sweep;
/// the parent/child relation is the enumeration itself, so no PPID re-check is needed.
/// `protected_wd_pid` spares the run watchdog; `older_than_ticks` is the start-time threshold
/// in the same (seconds) unit as procStartTimeTicks.
pub fn killAdoptedOrphans(
    self_pid: i32,
    exclude: []const i32,
    protected_groups: []const i32,
    older_than_ticks: ?u64,
    protected_wd_pid: i32,
) void {
    const budget_deadline: i64 = monotonicMs() + 10_000;
    var pids_buf: [1024]i32 = undefined;
    const nbytes = proc_listchildpids(self_pid, &pids_buf, @sizeOf(@TypeOf(pids_buf)));
    if (nbytes <= 0) return;
    const count: usize = @intCast(@divTrunc(nbytes, @sizeOf(i32)));
    for (pids_buf[0..@min(count, pids_buf.len)]) |pid| {
        if (pid <= 0 or pid == self_pid) continue;
        if (pid == protected_wd_pid) continue;
        if (pidInList(exclude, pid)) continue;

        var info: ProcBsdInfo = std.mem.zeroes(ProcBsdInfo);
        const rc = proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, @sizeOf(ProcBsdInfo));
        if (rc != @sizeOf(ProcBsdInfo)) continue; // gone or unreadable: spare
        const pgrp: i32 = @intCast(info.pbi_pgid);
        if (pidInList(protected_groups, pgrp)) continue;

        if (older_than_ticks) |threshold| {
            if (info.pbi_start_tvsec == 0) continue; // unknown start: spare, never kill on unknown
            if (info.pbi_start_tvsec >= threshold) continue; // may belong to a live sibling
        }

        // Group kill only when the candidate verified as a group leader (same pgid guard as Linux).
        if (pgrp == pid) killGroup(pid);
        _ = kill(pid, SIGKILL);
        const now_ms = monotonicMs();
        if (now_ms < budget_deadline) {
            _ = reapChildBounded(pid, @min(budget_deadline, now_ms + 2_000));
        }
    }
}

/// Bounded WNOHANG reap loop (waitpid variant of the Linux wait4 loop; same contract).
pub fn reapChildBounded(pid: i32, deadline_ms: i64) i32 {
    var status: i32 = -1;
    while (true) {
        const wrc = waitpid(pid, &status, WNOHANG);
        if (wrc == pid) return status;
        if (wrc < 0) return -1; // ECHILD: not our child (already reaped elsewhere)
        if (deadline_ms - monotonicMs() <= 0) return -1;
        pollSleep();
    }
}

/// Non-blocking waitpid loop with deadline + WNOWAIT group sweep — the Darwin twin of the
/// Linux waitid(PEEK)+reap path. waitid's si_pid is read from the typed struct field
/// (SiginfoDarwin.si_pid), never from a raw byte offset: the Darwin siginfo_t layout differs
/// from the Linux one, and the offset-16 shortcut would misread it.
pub fn waitPidWithDeadline(pid: i32, deadline_ms: i64, isInterrupted: fn () bool) i32 {
    while (deadline_ms - monotonicMs() > 0) {
        if (isInterrupted()) {
            killGroup(pid);
            _ = kill(pid, SIGKILL);
            return reapChildBounded(pid, monotonicMs() + 2_000);
        }
        var si: SiginfoDarwin = std.mem.zeroes(SiginfoDarwin);
        const wrc = waitid(P_PID, @intCast(pid), &si, WEXITED_WAITID | WNOHANG | WNOWAIT);
        if (wrc != 0) return -1; // ECHILD: not our child — never kill a pid we do not own
        if (si.si_pid == pid) {
            killGroup(pid);
            return reapChildBounded(pid, monotonicMs() + 2_000);
        }
        pollSleep();
    }
    killGroup(pid);
    _ = kill(pid, SIGKILL);
    return reapChildBounded(pid, monotonicMs() + 2_000);
}

/// The 2 ms bounded-poll cadence of every wait loop in this PAL. Zig 0.17 has
/// no std.Thread.sleep, and the Linux twin loops on the raw nanosleep syscall;
/// Darwin loops on the libSystem one. EINTR is harmless here: the loop's own
/// deadline re-check bounds the wait either way.
fn pollSleep() void {
    var req: Timespec = .{ .sec = 0, .nsec = 2_000_000 };
    _ = nanosleep(&req, null);
}

/// Watchdog registry: OS-neutral contract (anonymous MAP_SHARED mapping). Implemented via
/// mmap(2) extern on Darwin; the View methods are byte-identical to the Linux ones so the
/// supervisor's register/unregister/killAll call sites are shared verbatim.
extern "c" fn mmap(addr: ?*anyopaque, len: usize, prot: i32, flags: i32, fd: i32, offset: i64) ?*anyopaque;
extern "c" fn munmap(addr: ?*anyopaque, len: usize) i32;
pub const PROT_READ_WRITE: i32 = 0x1 | 0x2;
pub const MAP_SHARED_ANON: i32 = 0x0001 | 0x1000; // MAP_SHARED | MAP_ANON

pub const PgidRegistry = struct {
    pub const CAPACITY: usize = 32;
    pub const MAP_LEN: usize = CAPACITY * 8;

    pub const View = struct {
        slots: []align(8) volatile u64,

        /// Atomic registration of a live worker's pgid (same contract as Linux: idempotent,
        /// pgid <= 1 rejected, fail-open on overflow).
        pub fn register(self: View, pgid: i32) bool {
            if (pgid <= 1) return false;
            const v: u64 = @intCast(pgid);
            for (self.slots) |*s| {
                if (@atomicLoad(u64, s, .monotonic) == v) return true;
            }
            for (self.slots) |*s| {
                if (@atomicLoad(u64, s, .monotonic) == 0) {
                    @atomicStore(u64, s, v, .release);
                    return true;
                }
            }
            return false;
        }

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

        pub fn killAll(self: View) void {
            for (self.slots) |*s| {
                const v = @atomicLoad(u64, s, .acquire);
                if (v == 0) continue;
                const pgid: i32 = @intCast(v);
                killGroup(pgid);
            }
        }
    };

    pub fn create() !View {
        const addr = mmap(null, MAP_LEN, PROT_READ_WRITE, MAP_SHARED_ANON, -1, 0);
        if (addr == null) return error.WatchdogMmapFailed;
        const bytes: [*]align(8) u8 = @ptrCast(@alignCast(addr.?));
        return .{ .slots = @as([*]volatile u64, @ptrCast(bytes))[0..CAPACITY] };
    }

    pub fn destroy(view: View) void {
        _ = munmap(@ptrCast(@volatileCast(view.slots.ptr)), MAP_LEN);
    }
};

/// Parent-death contract (ADR-004 layer 1) on Darwin. Before exec, the child
/// registers NOTE_EXIT watches for both its parent and itself, then forks a
/// tiny monitor. The monitor exits when the protected child exits; if the
/// parent exits first, it SIGKILLs the protected child and exits. Because the
/// kqueue registrations precede exec and cover both processes, no post-exec
/// Python thread/timer is needed and the fork-to-exec window is fail-closed.
pub fn armPdeathsigOn(parent_pid: i32) void {
    const protected_pid = getpid();
    const kq = kqueue();
    if (kq < 0) _exit(124);
    var changes = [_]Kevent{
        .{ .ident = @intCast(parent_pid), .filter = EVFILT_PROC, .flags = EV_ADD | EV_ENABLE | EV_CLEAR, .fflags = NOTE_EXIT, .data = 0, .udata = null },
        .{ .ident = @intCast(protected_pid), .filter = EVFILT_PROC, .flags = EV_ADD | EV_ENABLE | EV_CLEAR, .fflags = NOTE_EXIT, .data = 1, .udata = null },
    };
    if (parent_pid <= 1 or protected_pid <= 1 or kevent(kq, &changes, 2, null, 0, null) < 0) _exit(124);
    const monitor = fork();
    if (monitor < 0) _exit(124);
    if (monitor != 0) {
        _ = close(kq);
        // Close the retrospective-death race after arming, just like prctl.
        if (getppid() != parent_pid) _exit(125);
        return;
    }
    // kqueue descriptors are invalidated by fork on Darwin: create the
    // monitor's queue in the monitor itself. Also release inherited fds,
    // especially the CLOEXEC spawn-handshake writer: CLOEXEC does not close
    // at fork, so keeping it here makes every successful exec look like a
    // spawn timeout to the supervisor.
    _ = close(kq);
    closeAllFdsExcept(-1);
    const monitor_kq = kqueue();
    if (monitor_kq < 0) {
        if (kill(protected_pid, 0) == 0) _ = kill(protected_pid, SIGKILL);
        _exit(124);
    }
    if (kevent(monitor_kq, &changes, 2, null, 0, null) < 0) {
        if (kill(protected_pid, 0) == 0) _ = kill(protected_pid, SIGKILL);
        _exit(124);
    }
    while (true) {
        var ev: [1]Kevent = undefined;
        const n = kevent(monitor_kq, null, 0, &ev, 1, null);
        if (n < 0) {
            // An interrupted wait is retryable; a broken watch cannot prove
            // containment, so fail closed by killing the protected process.
            if (kill(protected_pid, 0) == 0) _ = kill(protected_pid, SIGKILL);
            _exit(124);
        }
        if (n == 0) continue;
        if (ev[0].ident == @as(usize, @intCast(protected_pid))) _exit(0); // protected process exited: disarm
        if (kill(protected_pid, 0) == 0) _ = kill(protected_pid, SIGKILL);
        _exit(0);
    }
}

/// Pre-exec arm entry point (kept symmetric with Linux for spawn_arm.zig).
pub fn armPdeathsigPreExec(parent_pid: i32) void {
    armPdeathsigOn(parent_pid);
}

pub const PollFd = extern struct { fd: i32, events: i16, revents: i16 };
extern "c" fn poll(fds: [*]PollFd, nfds: u32, timeout: i32) i32;

/// Spawns the run watchdog: a fork whose child blocks in kevent on the supervisor's
/// EVFILT_PROC/NOTE_EXIT watch, then walks the shared pgid registry with killpg.
/// A kqueue itself is not poll(POLLIN)-readable on Darwin; waiting for the
/// supervisor therefore uses kevent, never poll on the kqueue descriptor.
/// Same one-process-for-the-whole-run shape as Linux.
pub fn spawnBatchWatchdog(reg_view: PgidRegistry.View) ?i32 {
    const supervisor_pid = getpid();
    const wpid = fork();
    if (wpid < 0) return null;
    if (wpid != 0) return wpid;
    // ---- watchdog (single process; a child of the supervisor) ----
    _ = setpgid(0, 0);
    // A kqueue created before fork is invalid in the child on Darwin. Create
    // the watch here, after fork, and do not retain unrelated inherited fds.
    closeAllFdsExcept(-1);
    const sup_fd = openSupervisorDeathFd(supervisor_pid);
    if (sup_fd < 0) _exit(124);
    var ev: [1]Kevent = undefined;
    while (true) {
        const n = kevent(sup_fd, null, 0, &ev, 1, null);
        if (n < 0) break; // EBADF etc.: a non-blocking exit without cleanup
        if (n != 0) break; // NOTE_EXIT fired: the supervisor is dead
    }
    reg_view.killAll();
    _exit(0);
}

// ----------------------------------------------------------------------------
// Peer credentials (LOCAL_PEERCRED) and output channels (/dev/fd)
// ----------------------------------------------------------------------------

pub const PeerCredError = error{ PeerCredFailed, UnauthorizedPeer, WorkerPidMismatch };

/// Same (pid, uid, gid) triple as the Linux ucred, filled from xucred + LOCAL_PEERPID.
pub const Ucred = extern struct {
    pid: i32,
    uid: u32,
    gid: u32,
};

/// Verifies peer credentials of an accepted Unix domain socket connection on fd 199 of the
/// shim's worker: getsockopt(SOL_LOCAL, LOCAL_PEERCRED) gives uid/gid; the pid arrives via
/// getsockopt(SOL_LOCAL, LOCAL_PEERPID). The (uid, pid) checks and error classes are
/// identical to the Linux SO_PEERCRED path.
pub fn verifySocketPeerCred(conn_fd: fd_t, expected_uid: u32, expected_pid: ?i32) PeerCredError!Ucred {
    var xc: Xucred = std.mem.zeroes(Xucred);
    var opt_len: u32 = @sizeOf(Xucred);
    if (getsockopt(conn_fd, SOL_LOCAL, LOCAL_PEERCRED, &xc, &opt_len) != 0) {
        return error.PeerCredFailed;
    }
    if (xc.xu_uid != expected_uid) {
        return error.UnauthorizedPeer;
    }
    var peer_pid: i32 = -1;
    var pid_len: u32 = @sizeOf(i32);
    if (getsockopt(conn_fd, SOL_LOCAL, LOCAL_PEERPID, &peer_pid, &pid_len) != 0 or peer_pid <= 0) {
        // XNU caches LOCAL_PEERCRED at connect() in unp_peercred, but LOCAL_PEERPID reads
        // unp_conn->unp_pid, which becomes NULL if a fast Zygote-forked worker closes its
        // socket before accept4(). UID is already verified above, the socket lives in a
        // private 0700 dir, and decodeAndVerifyFrame still checks worker_pid + CRC32 on every frame.
        if (expected_pid) |epid| {
            peer_pid = epid;
        } else {
            return error.PeerCredFailed;
        }
    }
    if (expected_pid) |epid| {
        if (peer_pid != epid) {
            return error.WorkerPidMismatch;
        }
    }
    return .{ .pid = peer_pid, .uid = xc.xu_uid, .gid = if (xc.xu_ngroups > 0) xc.xu_groups[0] else 0xFFFFFFFF };
}

/// True when fcntl(F_GETPATH) can name this descriptor in the filesystem. Anonymous XNU
/// pipes have no vnode path: their stdout/stderr must stay usable through the original fd.
pub fn outputFdHasPath(std_fd: i32) bool {
    var path_buf: [MAXPATHLEN]u8 = undefined;
    return fcntl(std_fd, F_GETPATH, @as(*[MAXPATHLEN]u8, &path_buf)) == 0;
}

pub fn openPrivateOutputFd(std_fd: i32) ?fd_t {
    var orig_st: StatDarwin = undefined;
    if (stat64.fstat(std_fd, &orig_st) != 0) return null;

    var path_buf: [MAXPATHLEN]u8 = undefined;
    if (fcntl(std_fd, F_GETPATH, @as(*[MAXPATHLEN]u8, &path_buf)) != 0) return null;

    const fl = fcntl(std_fd, F_GETFL, @as(c_int, 0));
    if (fl < 0) return null;
    var flags: i32 = O_WRONLY | O_NONBLOCK | O_CLOEXEC;
    if (fl & O_APPEND != 0) flags |= O_APPEND;
    if (fl & O_DSYNC != 0) flags |= O_DSYNC;
    if (fl & O_SYNC != 0) flags |= O_SYNC;
    const fd = open(@ptrCast(&path_buf), flags);
    if (fd < 0) return null;

    var new_st: StatDarwin = undefined;
    if (stat64.fstat(fd, &new_st) != 0 or
        new_st.st_dev != orig_st.st_dev or new_st.st_ino != orig_st.st_ino)
    {
        _ = close(fd);
        return null;
    }
    const private_fl = fcntl(fd, F_GETFL, @as(c_int, 0));
    if (private_fl < 0 or (private_fl & O_NONBLOCK) == 0) {
        _ = close(fd);
        return null;
    }
    return fd;
}

// ----------------------------------------------------------------------------
// File metadata: fstatat(2)-based equivalents of the Linux statx helpers
// ----------------------------------------------------------------------------

const stat64 = if (builtin.cpu.arch == .x86_64)
    struct {
        fn fstat(fd: i32, sb: *StatDarwin) i32 {
            return @extern(*const fn (i32, *StatDarwin) callconv(.c) i32, .{ .name = "fstat$INODE64" })(fd, sb);
        }
        fn stat(path: [*:0]const u8, sb: *StatDarwin) i32 {
            return @extern(*const fn ([*:0]const u8, *StatDarwin) callconv(.c) i32, .{ .name = "stat$INODE64" })(path, sb);
        }
    }
else
    struct {
        extern "c" fn fstat(fd: i32, sb: *StatDarwin) i32;
        extern "c" fn stat(noalias path: [*:0]const u8, noalias sb: *StatDarwin) i32;
    };

/// struct stat (Darwin, 64-bit, sys/stat.h; _DARWIN_FEATURE_64_BIT_INODE default layout).
/// Only st_mode/st_size/st_dev/st_ino are consumed, but the full layout is kept so the
/// extern signature matches libSystem exactly.
pub const StatDarwin = extern struct {
    st_dev: i32,
    st_mode: u16,
    st_nlink: u16,
    st_ino: u64,
    st_uid: u32,
    st_gid: u32,
    st_rdev: i32,
    st_atimespec: Timespec,
    st_mtimespec: Timespec,
    st_ctimespec: Timespec,
    st_birthtimespec: Timespec,
    st_size: i64,
    st_blocks: i64,
    st_blksize: i32,
    st_flags: u32,
    st_gen: u32,
    st_lspare: i32,
    st_qspare: [2]i64,
};

pub const S_IFMT: u32 = 0o170000;
pub const S_IFIFO: u32 = 0o010000;
pub const S_IFCHR: u32 = 0o020000;
pub const S_IFDIR: u32 = 0o040000;
pub const S_IFBLK: u32 = 0o060000;
pub const S_IFREG: u32 = 0o100000;
pub const S_IFSOCK: u32 = 0o140000;

/// File size in bytes (0 on failure; same degradation contract as the Linux statx helper).
pub fn statFileSize(path_z: [*:0]const u8) u64 {
    var sb: StatDarwin = std.mem.zeroes(StatDarwin);
    if (stat64.stat(path_z, &sb) != 0) return 0;
    if (sb.st_size < 0) return 0;
    return @intCast(sb.st_size);
}

/// Mode bits of an open fd (null on failure; same contract as the Linux statx helper).
pub fn fstatMode(fd: fd_t) ?u32 {
    var sb: StatDarwin = std.mem.zeroes(StatDarwin);
    if (stat64.fstat(fd, &sb) != 0) return null;
    return sb.st_mode;
}

/// Mode bits of a path (null when absent or unstat-able; symlinks followed, as on Linux).
pub fn statModeBits(path: []const u8) ?u32 {
    var buf: [4096]u8 = undefined;
    const pz = std.fmt.bufPrintSentinel(&buf, "{s}", .{path}, 0) catch return null;
    var sb: StatDarwin = std.mem.zeroes(StatDarwin);
    if (stat64.stat(pz.ptr, &sb) != 0) return null;
    return sb.st_mode;
}

pub const DirKey = struct { dev: u64, ino: u64 };

/// Directory identity from the open fd's fstat (same visited-set contract as Linux).
pub fn dirKeyOfFd(fd: fd_t) ?DirKey {
    var sb: StatDarwin = std.mem.zeroes(StatDarwin);
    if (stat64.fstat(fd, &sb) != 0) return null;
    return .{ .dev = @intCast(@as(u32, @bitCast(sb.st_dev))), .ino = sb.st_ino };
}

pub fn isDirectoryProbe(path_slice: []const u8) bool {
    const mode = statModeBits(path_slice) orelse return false;
    return (mode & S_IFMT) == S_IFDIR;
}

pub fn isReg(mode_bits: u32) bool {
    return (mode_bits & S_IFMT) == S_IFREG;
}

pub fn isFifo(mode_bits: u32) bool {
    return (mode_bits & S_IFMT) == S_IFIFO;
}

pub fn isChr(mode_bits: u32) bool {
    return (mode_bits & S_IFMT) == S_IFCHR;
}

pub fn isBlk(mode_bits: u32) bool {
    return (mode_bits & S_IFMT) == S_IFBLK;
}

pub fn isSock(mode_bits: u32) bool {
    return (mode_bits & S_IFMT) == S_IFSOCK;
}

/// Raw O_NONBLOCK status bit (Darwin: 0x0004; sys/fcntl.h).
pub const O_NONBLOCK_BIT: u32 = O_NONBLOCK;

/// XNU socket.h / socket_private.h. MSG_DONTWAIT covers sblock; MSG_NBIO is
/// also required to make a full send buffer return EWOULDBLOCK instead of sbwait.
pub const MSG_DONTWAIT: u32 = 0x80;
pub const MSG_NBIO: u32 = 0x20000;
pub const MSG_NOSIGNAL: u32 = 0x80000;

/// Reads ru_maxrss of a reaped child. Darwin reports ru_maxrss in BYTES (Linux: KiB) —
/// the PAL normalizes at the call boundary so the supervisor prints one unit on every OS.
pub fn readMaxRssBytes(ru_maxrss_native: i64) i64 {
    return ru_maxrss_native; // already bytes on Darwin
}
