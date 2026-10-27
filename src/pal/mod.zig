//! Platform abstraction layer: every OS-specific decision of the supervisor is made behind
//! this module, so ADR-004 containment semantics stay auditable in one place per OS and the
//! dispatcher logic in main.zig stays platform-neutral.
//!
//! `impl` is the active backend (`linux.zig` / `darwin.zig`); both backends provide the exact
//! same symbol set with the exact same call contract, so main.zig compiles identically on
//! every supported OS without comptime switches scattered through the supervisor.

const std = @import("std");
const builtin = @import("builtin");

/// Low-level POSIX surface used by the existing supervisor. On Linux this is
/// literally std.os.linux (no wrapper, no runtime change); Darwin supplies the
/// same small call vocabulary through libSystem in posix_darwin.zig.
pub const posix = switch (builtin.os.tag) {
    .linux => std.os.linux,
    .macos => @import("posix_darwin.zig"),
    else => @compileError("ztest PAL: unsupported OS"),
};

pub const impl = switch (builtin.os.tag) {
    .linux => @import("linux.zig"),
    .macos => @import("darwin.zig"),
    else => @compileError("ztest PAL: unsupported OS (supported: Linux, macOS/Darwin)"),
};

// Process containment contract (ADR-004).
pub const PgidCheck = impl.PgidCheck;
pub const Ucred = impl.Ucred;
pub const PgidRegistry = impl.PgidRegistry;

pub const monotonicMs = impl.monotonicMs;
pub const pidfdOpen = impl.pidfdOpen;
pub const pidInList = impl.pidInList;

/// Milliseconds left until an absolute monotonic deadline, clamped to i32.
pub fn remainingMs(deadline_ms: i64) i32 {
    const rem = deadline_ms - monotonicMs();
    if (rem <= 0) return 0;
    if (rem > std.math.maxInt(i32)) return std.math.maxInt(i32);
    return @intCast(rem);
}
pub const checkPeerPgid = impl.checkPeerPgid;
pub const enableChildSubreaper = impl.enableChildSubreaper;
pub const killGroup = impl.killGroup;
pub const armPdeathsigOn = impl.armPdeathsigOn;
pub const armPdeathsigPreExec = impl.armPdeathsigPreExec;
pub const reapAdoptedChildren = impl.reapAdoptedChildren;
pub const killAdoptedOrphans = impl.killAdoptedOrphans;
pub const procStartTimeTicks = impl.procStartTimeTicks;
pub const countProcTasks = impl.countProcTasks;
pub const spawnBatchWatchdog = impl.spawnBatchWatchdog;

// Sockets / IPC: peer authentication is SO_PEERCRED on Linux, LOCAL_PEERCRED on Darwin;
// both deliver the same (pid, uid, gid) triple to the accept path.
pub const verifySocketPeerCred = impl.verifySocketPeerCred;

// Output sinks: a private nonblocking OFD for the supervisor's own copy of fd 1/2.
// Linux reopens via /proc/self/fd, Darwin via F_GETPATH; both preserve O_APPEND/DSYNC/SYNC.
pub const openPrivateOutputFd = impl.openPrivateOutputFd;

/// Linux can reopen an anonymous pipe through /proc/self/fd. XNU cannot: F_GETPATH fails
/// there, so main keeps writable anonymous-pipe output on the original fd instead of dropping it.
pub fn outputFdHasPath(std_fd: i32) bool {
    if (comptime builtin.os.tag == .macos) return impl.outputFdHasPath(std_fd);
    return true;
}

// File metadata: statx on Linux, fstatat on Darwin — same (size, mode, dir-key) answers.
pub const statFileSize = impl.statFileSize;
pub const fstatMode = impl.fstatMode;
pub const statModeBits = impl.statModeBits;
pub const canonicalizePath = impl.canonicalizePath;
pub const DirKey = impl.DirKey;
pub const LinuxDirent64 = impl.LinuxDirent64;
pub const dirKeyOfFd = impl.dirKeyOfFd;
pub const isDirectoryProbe = impl.isDirectoryProbe;
pub const isReg = impl.isReg;
pub const isFifo = impl.isFifo;
pub const isChr = impl.isChr;
pub const isBlk = impl.isBlk;
pub const isSock = impl.isSock;
pub const O_NONBLOCK_BIT = impl.O_NONBLOCK_BIT;
pub const MSG_DONTWAIT = impl.MSG_DONTWAIT;
pub const MSG_NBIO = impl.MSG_NBIO;
pub const MSG_NOSIGNAL = impl.MSG_NOSIGNAL;

// Spawn support (shared by main.zig's spawn machinery and spawn_arm.zig).
pub const reapChildBounded = impl.reapChildBounded;
pub const readMaxRssBytes = impl.readMaxRssBytes;

// A pidfd handle reads POLLIN on the process's death on Linux; on Darwin the same contract
// is fulfilled by a kqueue(2) fd holding an EVFILT_PROC/NOTE_EXIT watch on the target pid.
// The dispatcher therefore keeps one poll-driven exit-notification path on both kernels.
