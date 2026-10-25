const std = @import("std");
const linux = std.os.linux;
const spawn_arm = @import("spawn_arm.zig");
const spawnArmed = spawn_arm.spawnArmed;
const builtin = @import("builtin");

const pyfiles = @import("pyfiles.zig");
const PyFileScan = pyfiles.PyFileScan;
const PyFilePatterns = pyfiles.PyFilePatterns;
const defaultPyFilePatterns = pyfiles.defaultPyFilePatterns;
const scanIniNorecursedirs = pyfiles.scanIniNorecursedirs;
const scanTomlNorecursedirs = pyfiles.scanTomlNorecursedirs;
const loadNorecursedirs = pyfiles.loadNorecursedirs;
const norecurseMatch = pyfiles.norecurseMatch;
const globMatch = pyfiles.globMatch;
const isTestFileNamePats = pyfiles.isTestFileNamePats;
const addPyFilePattern = pyfiles.addPyFilePattern;
const splitPyFileValue = pyfiles.splitPyFileValue;
const scanIniPythonFiles = pyfiles.scanIniPythonFiles;
const scanTomlPythonFiles = pyfiles.scanTomlPythonFiles;
const readFileIntoBuf = pyfiles.readFileIntoBuf;
const loadPythonFilePatterns = pyfiles.loadPythonFilePatterns;
const isIgnoredDir = pyfiles.isIgnoredDir;

const climod = @import("cli.zig");
const findPythonBinary = climod.findPythonBinary;
const timeoutMicros = climod.timeoutMicros;
const workerReadTimeoutMs = climod.workerReadTimeoutMs;
const parsePositiveTimeout = climod.parsePositiveTimeout;
const parseJobsCount = climod.parseJobsCount;
const ParsedCli = climod.ParsedCli;
const parseCliArgs = climod.parseCliArgs;

// siginfo_t.si_pid sits at offset 16 on 64-bit Linux (x86_64 and aarch64:
// si_signo i32 [0..4], si_errno i32 [4..8], si_code i32 [8..12], _pad0 i32 [12..16], si_pid i32 [16..20]).
// On any other architecture compilation must fail rather than silently reading the wrong offset.
comptime {
    if (builtin.cpu.arch != .x86_64 and builtin.cpu.arch != .aarch64) {
        @compileError("si_pid offset 16 verified for x86_64 and aarch64 Linux only");
    }
}

fn siginfoPid(buf: *const [128]u8) i32 {
    return std.mem.readInt(i32, buf[16..20], .little);
}

pub const ZTST_MAGIC: u32 = 0x5A545354; // "ZTST"
pub const IPC_HEADER_SIZE: usize = 20;
pub const EMBEDDED_SHIM: []const u8 = @embedFile("shim/ztest_shim.py");

var g_interrupted: std.atomic.Value(u32) = .init(0);

fn onTerminationSignal(sig: linux.SIG) callconv(.c) void {
    _ = sig;
    g_interrupted.store(1, .release);
}

pub fn isInterrupted() bool {
    return g_interrupted.load(.acquire) != 0;
}

pub fn installSignalHandlers() void {
    var ign_act: linux.Sigaction = .{
        .handler = .{ .handler = linux.SIG.IGN },
        .mask = linux.sigemptyset(),
        .flags = 0,
    };
    _ = linux.sigaction(linux.SIG.PIPE, &ign_act, null);

    var term_act: linux.Sigaction = .{
        .handler = .{ .handler = onTerminationSignal },
        .mask = linux.sigemptyset(),
        .flags = 0,
    };
    _ = linux.sigaction(linux.SIG.INT, &term_act, null);
    _ = linux.sigaction(linux.SIG.TERM, &term_act, null);
    _ = linux.sigaction(linux.SIG.HUP, &term_act, null);
}

pub fn monotonicMs() i64 {
    var ts: linux.timespec = .{ .sec = 0, .nsec = 0 };
    _ = linux.clock_gettime(.MONOTONIC, &ts);
    return @as(i64, ts.sec) * 1000 + @divTrunc(@as(i64, ts.nsec), 1_000_000);
}

pub fn remainingMs(deadline_ms: i64) i32 {
    const rem = deadline_ms - monotonicMs();
    if (rem <= 0) return 0;
    if (rem > std.math.maxInt(i32)) return std.math.maxInt(i32);
    return @intCast(rem);
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

fn pidInList(list: []const i32, pid: i32) bool {
    for (list) |p| {
        if (p == pid) return true;
    }
    return false;
}

/// pidfd_open(2) via raw syscall: a pidfd pins the worker's underlying `struct pid`
/// (not the numeric pid — the number is released for reuse the moment the process is reaped) and
/// becomes readable (POLLIN) the moment the process EXITS, zombie state included. Reuse-safety of
/// our kill signals comes from the pinned zombie plus the kill(-pgid)-before-reap ordering, not
/// from the pidfd. Returns -1 on any failure (caller degrades to the deadline-only behavior).
pub fn pidfdOpen(pid: i32) linux.fd_t {
    if (pid <= 0) return -1;
    const rc = linux.syscall2(.pidfd_open, @as(usize, @intCast(pid)), 0);
    if (linux.errno(rc) != .SUCCESS) return -1;
    return @intCast(rc);
}

/// Selectively reaps zombie children adopted via PR_SET_CHILD_SUBREAPER, without recording
/// statuses: the kernel recycles a pid the moment it is reaped, so a bare-pid status map could
/// hand Phase 4 a stale entry for a colliding reused pid and fabricate a false CRASHED(9).
/// Registered (protected) worker zombies are never reaped here — active slots still signal those
/// pids (`kill(-pid)` group sweeps) until their Phase 4 transition, and reaping would release the
/// pid for reuse. The sweep peeks with WNOWAIT and reaps only unregistered adoptees; a bounded
/// seen-set breaks the peek cycle when only protected zombies remain.
pub fn reapAdoptedChildren(protected: []const i32) void {
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
        // The run watchdog counts as protected: the sweep never reaps it (the runMain defer
        // owns that); the seen-set breaks the cycle, else a WNOWAIT peek of a zombie watchdog would loop the sweep.
        if (pid == g_wd_pid or pidInList(protected, pid)) {
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

pub const ExitCode = enum(u8) {
    ok = 0,
    tests_failed = 1,
    interrupted = 2,
    internal_error = 3,
    usage_error = 4,
    no_tests_collected = 5,

    pub fn toU8(self: ExitCode) u8 {
        return @backingInt(self);
    }

    pub fn fromU8(v: u8) ExitCode {
        if (v > 5) return .internal_error;
        return @fromBackingInt(v);
    }
};

/// True when a FILE_DONE status marks a failed collection: the worker could not import or
/// compile the file, so none of its tests ran.
pub fn isCollectionErrorStatus(status: []const u8) bool {
    return std.mem.eql(u8, status, "COLLECTION_ERROR");
}

/// Session exit code with pytest's precedence: an interrupted run and a collection error both
/// exit 2 (a collection error outranks plain test failures), failures exit 1, an empty
/// selection exits 5 and a clean run exits 0.
pub fn sessionExitCode(
    interrupted: bool,
    collection_errors: u32,
    failed: u32,
    passed: u32,
    skipped: u32,
    collected: u32,
) u8 {
    if (interrupted) return ExitCode.interrupted.toU8();
    if (collection_errors > 0) return ExitCode.interrupted.toU8();
    if (failed > 0) return ExitCode.tests_failed.toU8();
    if (passed == 0 and skipped == 0 and collected == 0) return ExitCode.no_tests_collected.toU8();
    return ExitCode.ok.toU8();
}

/// Session tallies mirrored for the interrupt path. main() unwinds through
/// error.Interrupted, where the local totals of runMain are already out of
/// reach, so each finalized file reports itself here as well. A SIGINT must
/// not leave the operator with a bare status code and zero bytes of output.
pub const PartialTally = struct {
    passed: u32 = 0,
    failed: u32 = 0,
    skipped: u32 = 0,
    files: u32 = 0,
};

var g_partial: PartialTally = .{};

/// Mirror one finalized file into the interrupt-visible tallies. The failure
/// count matches what a completed session summary would show: a crashed file
/// adds one failure on top of the failures its own frames reported, and a
/// FAILED outcome with failed==0 still counts as one.
pub fn notePartialTally(summary: FileRunSummary, outcome: WorkerOutcome) void {
    g_partial.files +%= 1;
    g_partial.passed +%= summary.passed;
    g_partial.skipped +%= summary.skipped;
    var f = summary.failed;
    if (outcome == .crashed or (outcome == .failed and f == 0)) f +%= 1;
    g_partial.failed +%= f;
}

/// Replace the (never delivered) FILE_DONE counts with the per-test frame
/// tallies. Only for a file that never reported FILE_DONE: that file's frames
/// are the verified record of everything that ran before the worker died, and
/// without this fold a crash would report passed=0 even though earlier tests
/// verifiably passed. A file with FILE_DONE keeps its authoritative counts.
pub fn foldFrameTallies(summary: *FileRunSummary) void {
    if (summary.got_file_done) return;
    summary.passed = summary.frame_passed;
    summary.failed = summary.frame_failed;
    summary.skipped = summary.frame_skipped;
}

/// The partial-tally line for an interrupted session (SIGINT/SIGTERM). Written
/// to stderr: it is a diagnostic for the operator, it must survive a piped or
/// redirected stdout, and it never pollutes the --llm JSONL stream on stdout.
pub fn printInterruptSummary() void {
    var buf: [192]u8 = undefined;
    const msg = std.fmt.bufPrint(
        &buf,
        "ztest: interrupted ({d} passed, {d} failed, {d} skipped so far, files finished: {d})\n",
        .{ g_partial.passed, g_partial.failed, g_partial.skipped, g_partial.files },
    ) catch return;
    writeStderr(msg);
}

pub const WorkerOutcome = enum(u8) {
    passed = 0,
    no_tests_selected = 1,
    failed = 2,
    crashed = 3,

    pub fn code(self: WorkerOutcome) u8 {
        return @backingInt(self);
    }
};

pub const IpcHeader = struct {
    magic: u32,
    worker_pid: i32,
    seq_no: u32,
    payload_len: u32,
    crc32: u32,
};

pub const IpcError = error{
    InvalidMagic,
    WorkerPidMismatch,
    PayloadTooLarge,
    CrcMismatch,
    SequenceMismatch,
    UnexpectedEof,
    MalformedJsonPayload,
    ReadTimeout,
    ReadFailed,
    WriteFailed,
    WriteTimeout,
    PeerCredFailed,
    UnauthorizedPeer,
    PgidInvariantFailed,
    LineTooLong,
    Interrupted,
};

pub fn computeFrameCrc32(hdr_prefix_16: *const [16]u8, payload: []const u8) u32 {
    var hasher = std.hash.Crc32.init();
    hasher.update(hdr_prefix_16);
    hasher.update(payload);
    return hasher.final();
}

/// Decodes the 20-byte ZTST frame header and verifies CRC32 over header[0..16] + payload.
pub fn decodeAndVerifyFrame(
    hdr_bytes: *const [IPC_HEADER_SIZE]u8,
    payload: []const u8,
    expected_seq: ?u32,
    expected_pid: ?i32,
) IpcError!IpcHeader {
    const magic = std.mem.readInt(u32, hdr_bytes[0..4], .little);
    if (magic != ZTST_MAGIC) return error.InvalidMagic;

    const worker_pid = std.mem.readInt(i32, hdr_bytes[4..8], .little);
    const seq_no = std.mem.readInt(u32, hdr_bytes[8..12], .little);
    const payload_len = std.mem.readInt(u32, hdr_bytes[12..16], .little);
    const crc32 = std.mem.readInt(u32, hdr_bytes[16..20], .little);

    if (payload_len > 16 * 1024 * 1024) return error.PayloadTooLarge;
    if (payload.len != payload_len) return error.UnexpectedEof;
    if (expected_seq) |exp| {
        if (seq_no != exp) return error.SequenceMismatch;
    }
    if (expected_pid) |epid| {
        if (worker_pid != epid) return error.WorkerPidMismatch;
    }

    const computed_crc = computeFrameCrc32(hdr_bytes[0..16], payload);
    if (computed_crc != crc32) return error.CrcMismatch;

    return .{
        .magic = magic,
        .worker_pid = worker_pid,
        .seq_no = seq_no,
        .payload_len = payload_len,
        .crc32 = crc32,
    };
}

/// Encodes a 20-byte ZTST frame header for testing and loopback verification.
pub fn encodeFrameHeader(worker_pid: i32, seq_no: u32, payload: []const u8) [IPC_HEADER_SIZE]u8 {
    var hdr: [IPC_HEADER_SIZE]u8 = @splat(0);
    std.mem.writeInt(u32, hdr[0..4], ZTST_MAGIC, .little);
    std.mem.writeInt(i32, hdr[4..8], worker_pid, .little);
    std.mem.writeInt(u32, hdr[8..12], seq_no, .little);
    std.mem.writeInt(u32, hdr[12..16], @intCast(payload.len), .little);
    const crc = computeFrameCrc32(hdr[0..16], payload);
    std.mem.writeInt(u32, hdr[16..20], crc, .little);
    return hdr;
}

/// Positive Proof of Pass evaluator: raw_wait_status < 0 counts as failure.
pub fn evaluateWorkerOutcome(
    got_file_done: bool,
    file_failed_count: u32,
    file_status: []const u8,
    raw_wait_status: i32,
) WorkerOutcome {
    if (raw_wait_status < 0) return .crashed;

    const st_u32: u32 = @bitCast(raw_wait_status);
    const signaled = linux.W.IFSIGNALED(st_u32);
    if (signaled) return .crashed;

    const exited = linux.W.IFEXITED(st_u32);
    if (!exited) return .crashed;

    const exit_code = linux.W.EXITSTATUS(st_u32);
    if (!got_file_done) return .crashed;

    if (file_failed_count > 0 or std.mem.eql(u8, file_status, "FAILED") or std.mem.eql(u8, file_status, "COLLECTION_ERROR")) {
        return .failed;
    }

    if (exit_code != 0) {
        return .crashed;
    }

    if (std.mem.eql(u8, file_status, "NO_TESTS_SELECTED")) {
        return .no_tests_selected;
    }

    if (std.mem.eql(u8, file_status, "PASSED") or std.mem.eql(u8, file_status, "SKIPPED") or std.mem.eql(u8, file_status, "COLLECTED")) {
        return .passed;
    }

    return .crashed;
}

/// Safe JSON field extractors that never panic on untrusted/malformed worker frames.
pub fn jsonGetString(obj: std.json.ObjectMap, key: []const u8, default: []const u8) []const u8 {
    if (obj.get(key)) |v| {
        return switch (v) {
            .string => |s| s,
            else => default,
        };
    }
    return default;
}

pub fn jsonGetU32(obj: std.json.ObjectMap, key: []const u8) IpcError!u32 {
    if (obj.get(key)) |v| {
        return switch (v) {
            .integer => |n| std.math.cast(u32, n) orelse error.MalformedJsonPayload,
            else => error.MalformedJsonPayload,
        };
    }
    return 0;
}

const LinuxDirent64 = extern struct {
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
    const dir_fd: linux.fd_t = @intCast(open_rc);
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

pub fn isTestFileName(name: []const u8) bool {
    if (!std.mem.endsWith(u8, name, ".py")) return false;
    if (std.mem.startsWith(u8, name, "test_")) return true;
    if (std.mem.endsWith(u8, name, "_test.py")) return true;
    return false;
}

fn isDirectoryProbe(path_slice: []const u8) bool {
    var pbuf: [4096]u8 = @splat(0);
    const pz = std.fmt.bufPrintSentinel(&pbuf, "{s}", .{path_slice}, 0) catch return false;
    const rc = linux.open(pz.ptr, .{ .ACCMODE = .RDONLY, .DIRECTORY = true, .CLOEXEC = true }, 0);
    if (linux.errno(rc) == .SUCCESS) {
        _ = linux.close(@intCast(rc));
        return true;
    }
    return false;
}

/// Directory identity for the discovery visited-set: the key is taken from
/// fstat of the already-open directory fd, i.e. after the kernel resolved any symlink chain on
/// the path. Two different symlink paths into the same directory collapse to one (st_dev, st_ino)
/// key, so directory cycles (a→., b→.) are pruned on entry instead of recursing exponentially.
const DirKey = struct { dev: u64, ino: u64 };

/// statx(2) on an open fd; null on syscall failure, letting discovery degrade to
/// untracked recursion (an outcome statx on an open directory fd practically never produces).
fn dirKeyOfFd(fd: linux.fd_t) ?DirKey {
    var stx: linux.Statx = undefined;
    const rc = linux.statx(fd, "", linux.AT.EMPTY_PATH, .{ .INO = true }, &stx);
    if (linux.errno(rc) != .SUCCESS) return null;
    const dev: u64 = (@as(u64, stx.dev_major) << 32) | @as(u64, stx.dev_minor);
    return .{ .dev = dev, .ino = stx.ino };
}

/// Discovery diagnostics: a traversal error (getdents64 failure, path
/// too long) is not silently treated as complete coverage. Design decision (documented): warn on
/// stderr and keep going instead of failing the run — one hostile/broken subtree cannot abort a
/// whole test session — but the operator is told coverage may be incomplete. Best-effort: if the
/// warning itself cannot be formatted, it is dropped (supervision outranks diagnostics).
fn warnDiscovery(path: []const u8, detail: []const u8) void {
    var buf: [512]u8 = undefined;
    const line = std.fmt.bufPrint(&buf, "ztest: discovery warning: {s}: {s} (coverage may be incomplete)\n", .{ path, detail }) catch return;
    writeStderr(line);
}

/// Recursively discovers `test_*.py` and `*_test.py` files with leak-free allocation guards.
/// Symlink-cycle-safe via a (st_dev, st_ino) visited-set keyed on the resolved
/// directory; isInterrupted() is checked in the enumeration loop and before every recursion so
/// SIGINT/SIGTERM interrupt discovery promptly (error.Interrupted -> exit code 2); traversal
/// errors are reported on stderr instead of masquerading as "directory fully read".
// A directory with a conftest.py that declares pytest_collect_file marks a
// custom-collector region: every non-.py file beneath it is handed to the
// worker for hook evaluation (the shim decides per-file via the chain, None → NO_TESTS_SELECTED).
fn conftestDeclaresCollectFile(dir_path: []const u8) bool {
    var pbuf: [4096]u8 = undefined;
    const pz = std.fmt.bufPrintSentinel(&pbuf, "{s}/conftest.py", .{std.mem.trimEnd(u8, dir_path, "/")}, 0) catch return false;
    var buf: [262144]u8 = undefined;
    const n = readFileIntoBuf(pz.ptr, buf[0..]) orelse return false;
    return std.mem.indexOf(u8, buf[0..n], "pytest_collect_file") != null;
}

pub fn discoverTestFiles(gpa: std.mem.Allocator, root_path: []const u8, out_files: *std.ArrayListUnmanaged([]u8)) !void {
    var visited: std.AutoHashMapUnmanaged(DirKey, void) = .empty;
    defer visited.deinit(gpa);
    // python_files from the target's ini config (replaces the default, pytest parity)
    var pats: PyFilePatterns = .{};
    loadPythonFilePatterns(root_path, &pats);
    // norecursedirs from ini (replaces the built-in set)
    var nrp: PyFilePatterns = .{};
    const has_norecurse = loadNorecursedirs(root_path, &nrp);
    if (pats.overflow or nrp.overflow) return error.TooManyPatterns;
    // Custom-collector scope (subdirectory target): the scope is
    // inherited top-down — with a target below a conftest.py declaring the hook
    // (ztest tests/examplefiles/abnf) the ancestor chain must be walked too,
    // otherwise the scope is lost and non-.py files never reach the worker
    // (pytest collects them via the conftest ancestors). The initial scope = or
    // over the ancestors of root_path from cwd down to root_path's parent (plus
    // the root itself — in Inner).
    const initial_cf_scope = ancestorHasCollectHook(root_path);
    return discoverTestFilesInner(gpa, root_path, out_files, &visited, &pats, initial_cf_scope, if (has_norecurse) &nrp else null);
}
fn ancestorHasCollectHook(root_path: []const u8) bool {
    // pytest loads the conftest chain from rootdir down; our walk starts from
    // root_path — the target's ancestors (including cwd itself) must contribute
    // the hook scope, else a root conftest with pytest_collect_file is lost
    // for any subdirectory target.
    var buf: [4096]u8 = @splat(0);
    const cwd_rc = linux.getcwd(&buf, buf.len);
    if (linux.errno(cwd_rc) != .SUCCESS) return false;
    const cwd_len = std.mem.indexOfScalar(u8, &buf, 0) orelse buf.len;
    const cwd = buf[0..cwd_len];
    if (conftestDeclaresCollectFile(cwd)) return true;
    // Normalize root into an absolute path for comparison with the ancestors.
    var abs_buf: [4096]u8 = undefined;
    var abs_root = if (root_path.len > 0 and root_path[0] == '/')
        root_path
    else blk: {
        const j = std.fmt.bufPrint(&abs_buf, "{s}/{s}", .{ cwd, root_path }) catch return false;
        break :blk j;
    };
    // ".."-normalization: a `..` target from tests/data must see the conftest one
    // level above; returns false on buffer overflow.
    var norm_buf: [4096]u8 = undefined;
    var nlen: usize = 0;
    var seg_it = std.mem.splitScalar(u8, abs_root, '/');
    while (seg_it.next()) |seg| {
        if (seg.len == 0 or std.mem.eql(u8, seg, ".")) continue;
        if (std.mem.eql(u8, seg, "..")) {
            if (nlen > 1) {
                const cut = std.mem.lastIndexOfScalar(u8, norm_buf[0 .. nlen - 1], '/') orelse 0;
                nlen = cut;
            }
            continue;
        }
        if (nlen + seg.len + 1 >= norm_buf.len) return false;
        norm_buf[nlen] = '/';
        nlen += 1;
        @memcpy(norm_buf[nlen .. nlen + seg.len], seg);
        nlen += seg.len;
    }
    if (nlen == 0) {
        norm_buf[0] = '/';
        nlen = 1;
    }
    abs_root = norm_buf[0..nlen];
    // Segment boundary: "/tmp/auditX" is not a descendant of "/tmp/audit".
    if (!std.mem.startsWith(u8, abs_root, cwd) or (abs_root.len > cwd.len and abs_root[cwd.len] != '/')) {
        // Target outside cwd (absolute path): pytest loads the conftest chain from
        // the target's own ancestors (rootdir is computed from the arguments) —
        // walk the ancestors of root_path up to /.
        if (abs_root.len >= 4096) return false;
        var anc_buf: [4096]u8 = undefined;
        @memcpy(anc_buf[0..abs_root.len], abs_root);
        var alen = abs_root.len;
        while (std.mem.lastIndexOfScalar(u8, anc_buf[0..alen], '/')) |cut| {
            if (cut == 0) break; // "/" — not checked
            alen = cut;
            if (conftestDeclaresCollectFile(anc_buf[0..alen])) return true;
        }
        return false;
    }
    // Ancestors between cwd and root: cwd/a/b/c → check cwd/a, cwd/a/b (the root
    // itself is checked in Inner). The root's parent is included too (a conftest in
    // tests/ is visible for the target tests/examplefiles).
    var rel = abs_root[cwd.len..];
    if (rel.len > 0 and rel[0] == '/') rel = rel[1..];
    var path_buf: [4096]u8 = undefined;
    @memcpy(path_buf[0..cwd.len], cwd);
    var plen = cwd.len;
    var it = std.mem.splitScalar(u8, rel, '/');
    while (it.next()) |seg| {
        if (seg.len == 0) continue;
        const is_last = it.peek() == null;
        if (is_last) break; // the root itself — checked in Inner
        path_buf[plen] = '/';
        plen += 1;
        @memcpy(path_buf[plen .. plen + seg.len], seg);
        plen += seg.len;
        if (conftestDeclaresCollectFile(path_buf[0..plen])) return true;
    }
    return false;
}

/// Compact duplicate CLI targets in place, keeping the first occurrence. Strings are owned by the caller (args arena) — nothing is freed here. O(n²) on
/// purpose: the target list is tiny, and targets are not sorted so duplicates are not adjacent.
/// `ztest tests tests` then discovers each file exactly once.
pub fn dedupTargets(gpa: std.mem.Allocator, targets: *std.ArrayListUnmanaged([]const u8)) void {
    _ = gpa;
    var w: usize = 0;
    outer: for (targets.items) |t| {
        for (targets.items[0..w]) |kept| {
            if (std.mem.eql(u8, t, kept)) continue :outer;
        }
        targets.items[w] = t;
        w += 1;
    }
    targets.shrinkRetainingCapacity(w);
}

/// Deduplicate the sorted discovered file list: overlapping targets
/// (`ztest tests tests/sub`) make `tests/sub/test_x.py` appear once per target. After the sort duplicates are adjacent, so one
/// linear pass reclaims them (the canonical-path guarantee: discovery always builds paths by
/// joining the same root string, so identical canonical files yield byte-identical paths).
/// Each file must run exactly once: a worker runs the file to completion, so a duplicate entry
/// would re-execute every test in it and double-count the summary.
pub fn dedupSortedFileList(gpa: std.mem.Allocator, files: *std.ArrayListUnmanaged([]u8)) void {
    var w: usize = 0;
    for (files.items, 0..) |f, i| {
        if (i > 0 and std.mem.eql(u8, f, files.items[w - 1])) {
            gpa.free(f);
            continue;
        }
        files.items[w] = f;
        w += 1;
    }
    files.shrinkRetainingCapacity(w);
}

/// Canonicalize discovered file paths (kernel realpath, no libc: std's
/// realpath was removed in Zig 0.17) so that string-different but same-file entries
/// (`ztest ./tests tests` discovers `./tests/test_x.py` and `tests/test_x.py`) collapse in
/// the sort+dedup pass that follows. O_PATH open + readlinkat(fd, "") (AT_EMPTY_PATH,
/// Linux >= 2.6.39), falling back to /proc/self/fd/<n>. `file::nodeid` targets canonicalize
/// the file prefix only; a path that fails to resolve stays as-is (its error is reported
/// later by the worker/loader — canonicalization must never mask a bad target). Every
/// canonicalized replacement frees the string it replaces: no leaks.
pub fn canonicalizeFileList(gpa: std.mem.Allocator, files: *std.ArrayListUnmanaged([]u8)) !void {
    var link_buf: [4096]u8 = @splat(0);
    var path_buf: [64]u8 = @splat(0);
    for (files.items, 0..) |f, i| {
        var file_part: []const u8 = f;
        var selector: []const u8 = "";
        if (std.mem.indexOf(u8, f, "::")) |sep| {
            file_part = f[0..sep];
            selector = f[sep..];
        }
        var target_buf: [4096]u8 = @splat(0);
        const target_z = std.fmt.bufPrintSentinel(&target_buf, "{s}", .{file_part}, 0) catch continue;
        const open_rc = linux.openat(linux.AT.FDCWD, target_z.ptr, .{ .PATH = true, .CLOEXEC = true }, 0);
        if (linux.errno(open_rc) != .SUCCESS) continue;
        const fd: linux.fd_t = @intCast(open_rc);
        defer _ = linux.close(fd);

        var n: usize = 0;
        const rl_rc = linux.readlinkat(fd, "", &link_buf, link_buf.len);
        if (linux.errno(rl_rc) == .SUCCESS and rl_rc > 0) {
            n = @intCast(rl_rc);
        } else {
            const proc_z = std.fmt.bufPrintSentinel(&path_buf, "/proc/self/fd/{d}", .{fd}, 0) catch continue;
            const rl2_rc = linux.readlinkat(linux.AT.FDCWD, proc_z.ptr, &link_buf, link_buf.len);
            if (linux.errno(rl2_rc) != .SUCCESS or rl2_rc == 0) continue;
            n = @intCast(rl2_rc);
        }
        if (selector.len == 0) {
            const real = try gpa.dupe(u8, link_buf[0..n]);
            gpa.free(f);
            files.items[i] = real;
            continue;
        }
        const joined = std.mem.concat(gpa, u8, &.{ link_buf[0..n], selector }) catch continue;
        gpa.free(f);
        files.items[i] = joined;
    }
}

fn discoverTestFilesInner(gpa: std.mem.Allocator, root_path: []const u8, out_files: *std.ArrayListUnmanaged([]u8), visited: *std.AutoHashMapUnmanaged(DirKey, void), pats: *const PyFilePatterns, cf_scope: bool, norecurse: ?*const PyFilePatterns) !void {
    if (std.mem.indexOf(u8, root_path, "::") != null or std.mem.endsWith(u8, root_path, ".py")) {
        const duped = try gpa.dupe(u8, root_path);
        errdefer gpa.free(duped);
        try out_files.append(gpa, duped);
        return;
    }

    var path_buf: [4096]u8 = @splat(0);
    const path_z = std.fmt.bufPrintSentinel(&path_buf, "{s}", .{root_path}, 0) catch {
        warnDiscovery(root_path, "path longer than 4096 bytes");
        return;
    };

    const open_rc = linux.open(path_z.ptr, .{ .ACCMODE = .RDONLY, .DIRECTORY = true, .CLOEXEC = true }, 0);
    switch (linux.errno(open_rc)) {
        .SUCCESS => {},
        else => {
            const duped = try gpa.dupe(u8, root_path);
            errdefer gpa.free(duped);
            try out_files.append(gpa, duped);
            return;
        },
    }
    const dir_fd: linux.fd_t = @intCast(open_rc);
    defer _ = linux.close(dir_fd);

    // Cycle prune: the key comes from the open fd, so the kernel has already resolved
    // any symlink chain; a directory we have visited under any path is skipped entirely.
    if (dirKeyOfFd(dir_fd)) |key| {
        if (visited.contains(key)) return;
        try visited.put(gpa, key, {});
    }

    // The custom-collector region is inherited from ancestors and extended by the
    // current directory's conftest.py (pytest: a conftest governs its own subtree).
    const cf_here = cf_scope or conftestDeclaresCollectFile(root_path);

    var dirent_buf: [8192]u8 align(@alignOf(LinuxDirent64)) = @splat(0);
    var subdirs: std.ArrayListUnmanaged([]u8) = .empty;
    defer {
        for (subdirs.items) |s| gpa.free(s);
        subdirs.deinit(gpa);
    }

    while (true) {
        if (isInterrupted()) return error.Interrupted;
        const nread_rc = linux.getdents64(dir_fd, &dirent_buf, @intCast(dirent_buf.len));
        switch (linux.errno(nread_rc)) {
            .SUCCESS => {},
            else => {
                // A getdents64 error must not masquerade as end-of-directory
                // (that would fabricate complete coverage). Warn and stop this scan.
                var errbuf: [48]u8 = undefined;
                const detail = std.fmt.bufPrint(&errbuf, "getdents64 errno={d}", .{@backingInt(linux.errno(nread_rc))}) catch "getdents64 failed";
                warnDiscovery(root_path, detail);
                break;
            },
        }
        if (nread_rc == 0) break;

        var bpos: usize = 0;
        while (bpos < nread_rc) {
            const d: *const LinuxDirent64 = @ptrCast(@alignCast(&dirent_buf[bpos]));
            if (d.d_reclen == 0) break;
            const name_ptr: [*:0]const u8 = @ptrCast(&d.d_name[0]);
            const name = std.mem.span(name_ptr);
            bpos += d.d_reclen;

            if (std.mem.eql(u8, name, ".") or std.mem.eql(u8, name, "..")) continue;

            const joined = try std.fmt.allocPrint(gpa, "{s}/{s}", .{ std.mem.trimEnd(u8, root_path, "/"), name });
            var is_dir = (d.d_type == 4);
            var is_reg = (d.d_type == 8);
            if (d.d_type == 0 or d.d_type == 10) {
                if (isDirectoryProbe(joined)) {
                    is_dir = true;
                } else {
                    is_reg = true;
                }
            }

            if (is_dir) {
                // an explicit norecursedirs replaces the built-in isIgnoredDir
                const skip_dir = if (norecurse) |nr| norecurseMatch(nr, name, joined) else isIgnoredDir(name);
                if (!skip_dir) {
                    subdirs.append(gpa, joined) catch |err| {
                        gpa.free(joined);
                        return err;
                    };
                } else {
                    gpa.free(joined);
                }
            } else if (is_reg and isTestFileNamePats(name, pats)) {
                out_files.append(gpa, joined) catch |err| {
                    gpa.free(joined);
                    return err;
                };
            } else if (is_reg and cf_here and !std.mem.eql(u8, name, "conftest.py")) {
                // Under a custom-collector region all files go to the worker — pytest_collect_file
                // is called first even for .py files (data-driven *.py inputs); None → fallback
                // to .py collection.
                out_files.append(gpa, joined) catch |err| {
                    gpa.free(joined);
                    return err;
                };
            } else {
                gpa.free(joined);
            }
        }
    }

    for (subdirs.items) |sub| {
        // Check before every recursion so SIGINT/SIGTERM interrupts discovery promptly,
        // even deep inside a large tree (error.Interrupted -> exit code 2 via ExitCode.interrupted).
        if (isInterrupted()) return error.Interrupted;
        try discoverTestFilesInner(gpa, sub, out_files, visited, pats, cf_here, norecurse);
    }
}

fn lessThanStr(_: void, a: []u8, b: []u8) bool {
    return std.mem.order(u8, a, b) == .lt;
}

/// Bounded non-blocking output channel: supervisor writes never block on a
/// stalled or stopped downstream reader. Overflowing output falls into a 1 MiB backlog flushed on
/// later writes; beyond the cap, bytes are dropped and counted (supervision and kill paths always
/// outrank diagnostic output). The write strategy is chosen once from the
/// fstat type of the inherited fd, and the flags of a foreign descriptor are never modified.
///   .plain — regular file / block device (or unknown/failed fstat): ordinary blocking write() on
///            the original fd. A regular file has no backpressure and shares the caller's file
///            offset/O_APPEND, so interleaved writes by other holders of the same fd (and by the
///            workers, which inherit fd 1/2) concatenate instead of clobbering each other.
///   .ofd   — FIFO / character device (terminal, /dev/null): a private open file description via
/// /proc/self/fd, O_NONBLOCK|O_CLOEXEC + carried O_APPEND/DSYNC/SYNC.
///   .sock  — socket sink: /proc/self/fd does not reopen sockets (ENXIO), so we write on the
///            original fd with send(MSG_DONTWAIT|MSG_NOSIGNAL): non-blocking per call without
///            touching the socket's flags.
///   .drop  — FIFO/tty sink whose private OFD could not be reopened (e.g. read-only permissions):
///            writes are discarded and counted in dropped_bytes; a fifo/tty never degrades to a
///            blocking .plain write (supervision must not hang on an unwritable sink).
const MAX_OUT_BACKLOG: usize = 1 << 20;

const MSG_DONTWAIT: u32 = 0x40; // Linux x86-64: per-call non-blocking send
const MSG_NOSIGNAL: u32 = 0x4000; // Linux x86-64: no SIGPIPE on dead peer

const OutSinkMode = enum { plain, ofd, sock, drop };

const OutChan = struct {
    fd: linux.fd_t,
    mode: OutSinkMode = .plain,
    backlog: std.ArrayListUnmanaged(u8) = .empty,
    dropped_bytes: u64 = 0,
    write_err: bool = false,
};

var g_out_stdout: OutChan = .{ .fd = 1, .mode = .plain };
var g_out_stderr: OutChan = .{ .fd = 2, .mode = .plain };

/// Buffers a complete message before any byte of it has been written (`off == 0`).
/// If the message does not fit in the remaining `MAX_OUT_BACKLOG`, the entire
/// message is dropped so no partial prefix ever enters the backlog or wire.
fn outBufferWhole(ch: *OutChan, msg: []const u8) void {
    if (msg.len == 0) return;
    const room = MAX_OUT_BACKLOG -| ch.backlog.items.len;
    if (msg.len > room) {
        ch.dropped_bytes +|= msg.len;
        return;
    }
    ch.backlog.appendSlice(std.heap.page_allocator, msg) catch {
        ch.dropped_bytes +|= msg.len;
        ch.write_err = true;
        ch.mode = .drop;
        return;
    };
}

/// Buffers the remaining suffix `msg[off..]` of a message whose prefix `msg[0..off]`
/// (`off > 0`) was already written to the sink while `backlog` was empty.
/// Always admits the tail (even if `msg.len > MAX_OUT_BACKLOG`) so an in-flight
/// JSONL record is never torn in half and glued to a subsequent record; while this
/// tail is draining, subsequent messages hit `outBufferWhole` (`off == 0`) and are
/// either queued after it or dropped whole.
fn outBufferTail(ch: *OutChan, tail: []const u8) void {
    if (tail.len == 0) return;
    ch.backlog.appendSlice(std.heap.page_allocator, tail) catch {
        ch.dropped_bytes +|= tail.len;
        ch.write_err = true;
        ch.mode = .drop;
        return;
    };
}

/// One write attempt on the channel's sink: write() for plain/ofd, send(MSG_DONTWAIT|MSG_NOSIGNAL)
/// for socket sinks (per-call non-blocking, socket flags untouched).
fn outWriteOnce(ch: *const OutChan, buf: []const u8) usize {
    return switch (ch.mode) {
        .plain, .ofd => linux.write(ch.fd, buf.ptr, buf.len),
        .sock => linux.sendto(ch.fd, buf.ptr, buf.len, MSG_DONTWAIT | MSG_NOSIGNAL, null, 0),
        .drop => 0,
    };
}

fn outFlush(ch: *OutChan) void {
    if (ch.mode == .drop) {
        // Drop-mode sink: any residual backlog is unservable — count it and discard.
        ch.dropped_bytes +|= ch.backlog.items.len;
        ch.backlog.clearRetainingCapacity();
        return;
    }
    const items = ch.backlog.items;
    var off: usize = 0;
    var intr_spins: u32 = 0; // bounded: a signal storm must not pin the flusher
    while (off < items.len) {
        const rc = outWriteOnce(ch, items[off..]);
        switch (linux.errno(rc)) {
            .SUCCESS => off += rc,
            .INTR => {
                intr_spins += 1;
                if (intr_spins > 64) break;
                continue;
            },
            .AGAIN => break,
            else => {
                ch.write_err = true;
                ch.mode = .drop;
                ch.dropped_bytes +|= items.len - off;
                off = items.len; // EPIPE/EBADF: drop the backlog, keep supervising
                break;
            },
        }
    }
    if (off > 0) {
        const rest = items.len - off;
        std.mem.copyForwards(u8, ch.backlog.items[0..rest], items[off..]);
        ch.backlog.shrinkRetainingCapacity(rest);
    }
}

fn outWrite(ch: *OutChan, msg: []const u8) void {
    if (msg.len == 0) return;
    if (ch.mode == .drop) {
        // Unopenable fifo/tty sink: supervision output is discarded, never blocks.
        ch.dropped_bytes +|= msg.len;
        return;
    }
    if (ch.backlog.items.len > 0) outFlush(ch);
    if (ch.mode == .drop) {
        ch.dropped_bytes +|= msg.len;
        return;
    }
    if (ch.backlog.items.len > 0) {
        outBufferWhole(ch, msg);
        return;
    }
    var off: usize = 0;
    var intr_spins: u32 = 0;
    while (off < msg.len) {
        const rc = outWriteOnce(ch, msg[off..]);
        switch (linux.errno(rc)) {
            .SUCCESS => off += rc,
            .INTR => {
                intr_spins += 1;
                if (intr_spins > 64) {
                    if (off == 0) outBufferWhole(ch, msg) else outBufferTail(ch, msg[off..]);
                    return;
                }
                continue;
            },
            .AGAIN => {
                if (off == 0) outBufferWhole(ch, msg) else outBufferTail(ch, msg[off..]);
                return;
            },
            else => {
                ch.write_err = true;
                ch.mode = .drop;
                ch.dropped_bytes +|= msg.len - off;
                return;
            },
        }
    }
}

/// Bounded final drain: up to ~2 s of POLLOUT polling per channel, so a backpressured
/// sink gets a real chance to accept the backlog before exit, yet the final flush can never hang
/// the supervisor on a stalled reader. Whatever remains past the deadline is counted in
/// dropped_bytes and discarded — the drop note then reports the count.
fn outFlushBounded(ch: *OutChan, deadline_ms: i64) void {
    if (ch.mode == .drop) {
        ch.dropped_bytes +|= ch.backlog.items.len;
        ch.backlog.clearRetainingCapacity();
        return;
    }
    const items = ch.backlog.items;
    var off: usize = 0;
    var intr_spins: u32 = 0;
    while (off < items.len) {
        if (isInterrupted()) break;
        const rc = outWriteOnce(ch, items[off..]);
        switch (linux.errno(rc)) {
            .SUCCESS => off += rc,
            .INTR => {
                intr_spins += 1;
                if (intr_spins > 64) break;
                continue;
            },
            .AGAIN => {
                // Sink is full: wait for POLLOUT up to the shared deadline (bounded).
                if (remainingMs(deadline_ms) <= 0) break;
                var pfd = [_]linux.pollfd{.{ .fd = ch.fd, .events = linux.POLL.OUT | linux.POLL.HUP | linux.POLL.ERR, .revents = 0 }};
                const step_ms: i32 = @min(remainingMs(deadline_ms), 100);
                const prc = linux.poll(&pfd, 1, step_ms);
                if (linux.errno(prc) != .SUCCESS) break;
                continue;
            },
            else => {
                ch.write_err = true;
                ch.mode = .drop;
                ch.dropped_bytes +|= items.len - off;
                off = items.len; // EPIPE/EBADF: unservable sink — drop, keep going
                break;
            },
        }
    }
    if (off > 0) {
        const rest = items.len - off;
        std.mem.copyForwards(u8, ch.backlog.items[0..rest], items[off..]);
        ch.backlog.shrinkRetainingCapacity(rest);
    }
    // Backlog that survived the bounded drain is dropped and counted (the note must not lose it).
    const leftover = ch.backlog.items.len;
    if (leftover > 0) {
        ch.dropped_bytes +|= leftover;
        ch.write_err = true;
        ch.mode = .drop;
        ch.backlog.clearRetainingCapacity();
    }
}

fn outFlushAll() void {
    const drain_deadline = monotonicMs() + 2_000;
    outFlushBounded(&g_out_stdout, drain_deadline);
    outFlushBounded(&g_out_stderr, drain_deadline);
    if (g_out_stdout.dropped_bytes + g_out_stderr.dropped_bytes > 0) {
        var buf: [96]u8 = @splat(0);
        const note = std.fmt.bufPrint(&buf, "ztest: dropped {d} output bytes (reader backpressure)\n", .{g_out_stdout.dropped_bytes + g_out_stderr.dropped_bytes}) catch return;
        // The drop note must never be lost by the sink-mode machinery itself.
        // In .drop mode outWriteOnce no-ops, and on a momentarily-full .sock/.ofd sink a blind
        // write returns EAGAIN and discards — exactly the note reporting the drops. Gate the
        // one-shot delivery on a ZERO-TIMEOUT POLLOUT poll of the channel's current fd and write
        // RAW (no backlog, no retry): if the sink is writable the note goes out, otherwise it is
        // skipped and counted rather than silently swallowed by the channel.
        var pfd = [_]linux.pollfd{.{ .fd = g_out_stderr.fd, .events = linux.POLL.OUT | linux.POLL.HUP | linux.POLL.ERR, .revents = 0 }};
        const prc = linux.poll(&pfd, 1, 0);
        if (linux.errno(prc) == .SUCCESS and prc > 0 and (pfd[0].revents & linux.POLL.OUT) != 0) {
            _ = linux.write(g_out_stderr.fd, note.ptr, note.len);
        }
    }
}

// OSS-COLOR: color is emitted only on a real tty and is disabled by NO_COLOR
// (pytest parity: piped -v output stays machine-parseable).
var g_use_color: bool = true;

fn cRed() []const u8 {
    return if (g_use_color) "\x1b[31m" else "";
}
fn cGreen() []const u8 {
    return if (g_use_color) "\x1b[32m" else "";
}
fn cYellow() []const u8 {
    return if (g_use_color) "\x1b[33m" else "";
}
fn cReset() []const u8 {
    return if (g_use_color) "\x1b[0m" else "";
}

fn stdoutIsTty() bool {
    _ = std.posix.tcgetattr(1) catch return false;
    return true;
}

fn writeStdout(msg: []const u8) void {
    outWrite(&g_out_stdout, msg);
}

fn writeStderr(msg: []const u8) void {
    outWrite(&g_out_stderr, msg);
}

/// Polls descriptor for readability up to `deadline_ms` (absolute CLOCK_MONOTONIC deadline)
/// while checking `isInterrupted()` every 100ms so SIGINT/SIGTERM triggers immediate cleanup.
pub fn pollFdUntilDeadline(fd: linux.fd_t, deadline_ms: i64) IpcError!bool {
    var pfd = [_]linux.pollfd{.{
        .fd = fd,
        .events = linux.POLL.IN | linux.POLL.HUP | linux.POLL.ERR,
        .revents = 0,
    }};
    while (true) {
        if (isInterrupted()) return error.Interrupted;
        const rem = remainingMs(deadline_ms);
        if (rem <= 0) return false;
        const step_ms: i32 = @min(rem, 100);
        const rc = linux.poll(&pfd, 1, step_ms);
        switch (linux.errno(rc)) {
            .SUCCESS => {
                if (rc > 0) return true;
                if (remainingMs(deadline_ms) <= 0) return false;
            },
            .INTR => {
                if (isInterrupted()) return error.Interrupted;
                continue;
            },
            else => return error.ReadFailed,
        }
    }
}

pub const Ucred = extern struct {
    pid: i32,
    uid: u32,
    gid: u32,
};

/// Verifies peer credentials (`SO_PEERCRED`) of an accepted Unix domain socket connection.
pub fn verifySocketPeerCred(conn_fd: linux.fd_t, expected_uid: u32, expected_pid: ?i32) IpcError!Ucred {
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

/// Accepts a connection before `deadline_ms` and authenticates the peer via `SO_PEERCRED`.
/// Stale or mismatched connections in the backlog are closed and skipped until the expected peer connects.
/// Accepted sockets are `SOCK.NONBLOCK | SOCK.CLOEXEC` (head-of-line blocking).
/// When `require_pgid_leader` is set, the peer must satisfy `getpgid(pid) == pid` (one best-effort
/// `setpgid` retry, then the spawn is aborted) so the later `kill(-pid)` group sweep is guaranteed to work.
fn acceptAuthenticatedWithDeadline(
    listen_fd: linux.fd_t,
    expected_uid: u32,
    expected_pid: ?i32,
    deadline_ms: i64,
    require_pgid_leader: bool,
) IpcError!linux.fd_t {
    while (true) {
        const readable = try pollFdUntilDeadline(listen_fd, deadline_ms);
        if (!readable) return error.ReadTimeout;

        const acc_rc = linux.accept4(listen_fd, null, null, linux.SOCK.NONBLOCK | linux.SOCK.CLOEXEC);
        switch (linux.errno(acc_rc)) {
            .SUCCESS => {
                const conn_fd: linux.fd_t = @intCast(acc_rc);
                const cred = verifySocketPeerCred(conn_fd, expected_uid, expected_pid) catch |err| {
                    _ = linux.close(conn_fd);
                    if (err == error.WorkerPidMismatch or err == error.UnauthorizedPeer) {
                        continue;
                    }
                    return err;
                };
                if (require_pgid_leader and checkPeerPgid(cred.pid) == .violator) {
                    _ = linux.close(conn_fd);
                    return error.PgidInvariantFailed;
                }
                return conn_fd;
            },
            .INTR => {
                if (isInterrupted()) return error.Interrupted;
                continue;
            },
            .AGAIN => continue,
            else => return error.ReadFailed,
        }
    }
}

/// Result of the last-chance accept attempt that runs at an expired
/// per-slot accept deadline.
const DeadlineAccept = enum {
    /// A verified worker connection is installed in `aw` (`conn_fd`/`worker_pidfd` set, inactivity
    /// deadline re-armed): the slot must be serviced, not billed as a timeout.
    accepted,
    /// Nothing legitimate waiting in the backlog (EAGAIN, or only stale/foreign peers that were
    /// closed): the timeout transition stands.
    none,
    /// A connected peer violated the pgrp invariant: same kill transition as STEP 1.
    pgid_violation,
};

/// Non-blocking accept attempted before a slot is declared `timed_out` on its accept
/// deadline: a worker that connected while the supervisor was busy finalizing a sibling must not
/// be SIGKILLed as "never connected". One `accept4(O_NONBLOCK)` costs a syscall and never blocks.
///
/// Semantics are the STEP-1 accept semantics, restricted to what the deadline needs:
/// stale/foreign peers (wrong pid per SO_PEERCRED, or wrong uid) are closed and skipped until
/// the backlog is drained; the first verified worker is installed exactly like the normal accept
/// path (`g_wd_reg.register`, `pidfdOpen`, inactivity deadline re-armed on `worker_read_timeout_ms`
/// so the spawn latency cannot be billed as inactivity) and reported as `.accepted`; a peer whose
/// pgid is not its own pid is a `.pgid_violation` (the caller kills it).
/// The connection is drained by the normal poll path on the next loop iteration; no frame is
/// parsed here (this function cannot fail mid-frame).
fn acceptReadyWorkerAtDeadline(
    listen_fd: linux.fd_t,
    expected_uid: u32,
    worker_read_timeout_ms: i64,
    aw: *ActiveWorker,
) IpcError!DeadlineAccept {
    while (true) {
        const acc_rc = linux.accept4(listen_fd, null, null, linux.SOCK.NONBLOCK | linux.SOCK.CLOEXEC);
        switch (linux.errno(acc_rc)) {
            .SUCCESS => {
                const cfd: linux.fd_t = @intCast(acc_rc);
                const cred = verifySocketPeerCred(cfd, expected_uid, aw.worker_pid) catch |err| {
                    _ = linux.close(cfd);
                    if (err == error.WorkerPidMismatch or err == error.UnauthorizedPeer) continue; // stale backlog
                    return err;
                };
                if (checkPeerPgid(cred.pid) == .violator) {
                    _ = linux.close(cfd);
                    return .pgid_violation;
                }
                aw.conn_fd = cfd;
                aw.worker_pidfd = pidfdOpen(aw.worker_pid);
                if (g_wd_reg.slots.len > 0) {
                    _ = g_wd_reg.register(aw.worker_pid);
                }
                aw.deadline_ms = monotonicMs() + worker_read_timeout_ms;
                return .accepted;
            },
            .AGAIN => return .none, // backlog drained: nothing connected before the deadline
            .INTR => {
                if (isInterrupted()) return error.Interrupted;
                continue;
            },
            // Hard accept error (EMFILE/ENFILE/ECONNABORTED...): do not kill a worker we may not
            // have seen — the slot transitions to timed_out as usual.
            else => return .none,
        }
    }
}

fn createUnixListenSocket(sock_path: []const u8) !linux.fd_t {
    var unlink_buf: [256]u8 = @splat(0);
    const unlink_z = try std.fmt.bufPrintSentinel(&unlink_buf, "{s}", .{sock_path}, 0);
    _ = linux.unlink(unlink_z.ptr);

    const sock_rc = linux.socket(linux.AF.UNIX, linux.SOCK.STREAM | linux.SOCK.CLOEXEC | linux.SOCK.NONBLOCK, 0);
    if (linux.errno(sock_rc) != .SUCCESS) return error.SocketCreateFailed;
    const fd: linux.fd_t = @intCast(sock_rc);
    errdefer _ = linux.close(fd);

    var addr: linux.sockaddr.un = .{ .family = linux.AF.UNIX, .path = @splat(0) };
    if (sock_path.len >= addr.path.len) return error.SocketPathTooLong;
    @memcpy(addr.path[0..sock_path.len], sock_path);

    const bind_rc = linux.bind(fd, @ptrCast(&addr), @sizeOf(linux.sockaddr.un));
    if (linux.errno(bind_rc) != .SUCCESS) return error.SocketBindFailed;

    const listen_rc = linux.listen(fd, 64);
    if (linux.errno(listen_rc) != .SUCCESS) return error.SocketListenFailed;

    return fd;
}

fn readExactFdDeadline(fd: linux.fd_t, buf: []u8, deadline_ms: i64) IpcError!bool {
    var off: usize = 0;
    while (off < buf.len) {
        const ready = try pollFdUntilDeadline(fd, deadline_ms);
        if (!ready) return error.ReadTimeout;

        const rc = linux.read(fd, buf[off..].ptr, buf.len - off);
        switch (linux.errno(rc)) {
            .SUCCESS => {
                if (rc == 0) return if (off == 0) false else error.UnexpectedEof;
                off += rc;
            },
            .INTR => {
                if (isInterrupted()) return error.Interrupted;
                continue;
            },
            // Socket is SOCK.NONBLOCK: data was consumed by a racing drain; poll again.
            .AGAIN => continue,
            else => return error.ReadFailed,
        }
    }
    return true;
}

/// Non-blocking, deadline-bounded write with POLLOUT polling (unbounded ctrl writes).
/// Shares the caller's absolute transaction deadline; never blocks indefinitely on a stalled Zygote.
fn writeAllFdDeadline(fd: linux.fd_t, data: []const u8, deadline_ms: i64) IpcError!void {
    var off: usize = 0;
    while (off < data.len) {
        if (isInterrupted()) return error.Interrupted;
        const rem = remainingMs(deadline_ms);
        if (rem <= 0) return error.WriteTimeout;

        var pfd = [_]linux.pollfd{.{
            .fd = fd,
            .events = linux.POLL.OUT | linux.POLL.HUP | linux.POLL.ERR,
            .revents = 0,
        }};
        const step_ms: i32 = @min(rem, 100);
        const prc = linux.poll(&pfd, 1, step_ms);
        switch (linux.errno(prc)) {
            .SUCCESS => {},
            .INTR => continue,
            else => return error.WriteFailed,
        }
        if (prc == 0) continue;
        if ((pfd[0].revents & (linux.POLL.HUP | linux.POLL.ERR)) != 0 and (pfd[0].revents & linux.POLL.OUT) == 0) {
            return error.WriteFailed;
        }

        const rc = linux.write(fd, data[off..].ptr, data.len - off);
        switch (linux.errno(rc)) {
            .SUCCESS => off += rc,
            .INTR => continue,
            .AGAIN => continue,
            else => return error.WriteFailed,
        }
    }
}

/// Bounded line reader with 64 KiB cap and a single absolute `deadline_ms` across the entire line.
/// Returns `error.LineTooLong` if 64 KiB is exceeded without `\n`.
fn readLineFdDeadline(gpa: std.mem.Allocator, fd: linux.fd_t, deadline_ms: i64) ![]u8 {
    var list: std.ArrayListUnmanaged(u8) = .empty;
    errdefer list.deinit(gpa);
    var ch: [1]u8 = @splat(0);
    while (true) {
        if (list.items.len >= 65536) return error.LineTooLong;
        const ready = try pollFdUntilDeadline(fd, deadline_ms);
        if (!ready) return error.ReadTimeout;
        const rc = linux.read(fd, &ch, 1);
        switch (linux.errno(rc)) {
            .SUCCESS => {
                if (rc == 0) break;
                if (ch[0] == '\n') break;
                try list.append(gpa, ch[0]);
            },
            .INTR => {
                if (isInterrupted()) return error.Interrupted;
                continue;
            },
            .AGAIN => continue, // SOCK.NONBLOCK listen/conn sockets: re-poll
            else => return error.ReadFailed,
        }
    }
    return list.toOwnedSlice(gpa);
}

const FileRunSummary = struct {
    passed: u32 = 0,
    failed: u32 = 0,
    skipped: u32 = 0,
    deselected: u32 = 0,
    /// FILE_DONE carries a `collected` count on the collect-only path (post-deselection
    /// selection size), aggregated into total_collected for the pytest exit parity:
    /// --collect-only with 0 collected items must exit 5 (EXIT_NOTESTSCOLLECTED), not 0.
    /// COLLECTION_DONE also reports the post-selection size, so a file that dies later
    /// without FILE_DONE (worker crash) still reports what it really collected.
    collected: u32 = 0,
    /// Per-test frame tallies (TEST_PASS / TEST_FAIL / TEST_SKIP), streamed as tests finish:
    /// a worker that dies without FILE_DONE leaves a verified record of what already ran.
    /// FILE_DONE stays authoritative; the tallies are used only when it never arrives.
    frame_passed: u32 = 0,
    frame_failed: u32 = 0,
    frame_skipped: u32 = 0,
    got_file_done: bool = false,
    timed_out: bool = false,
    ipc_error: bool = false,
    status_buf: [32]u8 = @splat(0),
    status_len: usize = 0,
};

/// Progress classification of a parsed worker frame.
/// `.file_start` opens the import/collection window: the caller re-arms with the collection budget
/// `max(worker_read_timeout_ms, 600_000)` because conftest/module import (e.g. pandas/torch) is not
/// per-test time and must not be billed against `--timeout`, yet an eternally hanging import is still
/// bounded (pytest-timeout spirit: collection is not tariffed, but not unbounded either).
/// `.progress` — verified per-test heartbeat (`TEST_PASS`, `TEST_SKIP`, `TEST_FAIL`, `TEST_START`,
/// `TEST_END`, `TEARDOWN_START`, `COLLECT_BATCH`): re-arms the ordinary inactivity deadline.
/// `.none` — terminal/non-progress frames (no re-arm).
pub const FrameProgress = enum { none, progress, file_start };

/// Collection-window budget applied to FILE_START re-arms: 10 minutes, bounded.
pub const FILE_START_BUDGET_MS: i64 = 600_000;

/// Deadline delta for a frame classification given the ordinary per-test read timeout.
pub fn frameBudgetMs(worker_read_timeout_ms: i64, prog: FrameProgress) i64 {
    return if (prog == .file_start) @max(worker_read_timeout_ms, FILE_START_BUDGET_MS) else worker_read_timeout_ms;
}

// ============================================================================
// Session-wide short test summary info
// ============================================================================
// The human summary ends with pytest's `=== short test summary info ===`
// block: one `FAILED <nodeid> - <reason>` line per failed test, printed before
// the final summary line. Records are collected from TEST_FAIL frames as they
// stream in, so the block is already complete when a later file crashes. The
// storage is a bounded static ring because the frame loop's step arena is
// reset every iteration — nothing allocated there may survive it.
//
// Bounded storage must not lie about what it holds, so three properties are
// explicit rather than incidental:
//   * dedup keys are 128-bit hashes of the FULL nodeid (plus its length), never
//     the truncated display text, so two long nodeids that share a 512-byte
//     prefix stay two records;
//   * a nodeid or reason that does not fit its display buffer keeps a UTF-8-safe
//     prefix and gets a `…` marker, so truncation is visible in the line;
//   * failures that arrive after the ring fills are counted and reported as
//     `... N more failed-test reports not shown`. That number counts records,
//     not unique tests: proving uniqueness past the ring would need unbounded
//     storage (or a second ring whose eviction makes the count wrong again), so
//     the line claims exactly what is known. The block never looks complete
//     when it is not.
pub const MAX_SUMMARY_FAILS: usize = 256;
pub const SUMMARY_NODEID_MAX: usize = 512;
pub const SUMMARY_REASON_MAX: usize = 120;

pub const FailedTestNote = struct { nodeid: []const u8, reason: []const u8 };

/// Dedup key for one failed test: two 64-bit FNV-1a passes over the full nodeid
/// (one forward, one over the reversed bytes with a different basis) plus the
/// byte length. 128 bits of identity: a collision would have to agree on both
/// passes and the length, and only ever merges two display lines — it never
/// inflates or deflates the run's counters.
pub const NodeIdIdentity = struct { h1: u64, h2: u64, len: usize };

const FNV_OFFSET_BASIS: u64 = 0xcbf29ce484222325;
const FNV_ALT_BASIS: u64 = 0x9e3779b97f4a7c15;
const FNV_PRIME: u64 = 0x100000001b3;

fn fnv1a64(seed: u64, bytes: []const u8, reverse: bool) u64 {
    var h: u64 = seed;
    var i: usize = 0;
    while (i < bytes.len) : (i += 1) {
        const b = if (reverse) bytes[bytes.len - 1 - i] else bytes[i];
        h ^= b;
        h *%= FNV_PRIME;
    }
    return h;
}

pub fn nodeIdIdentity(nodeid: []const u8) NodeIdIdentity {
    return .{
        .h1 = fnv1a64(FNV_OFFSET_BASIS, nodeid, false),
        .h2 = fnv1a64(FNV_ALT_BASIS, nodeid, true),
        .len = nodeid.len,
    };
}

fn identityEq(a: NodeIdIdentity, b: NodeIdIdentity) bool {
    return a.h1 == b.h1 and a.h2 == b.h2 and a.len == b.len;
}

/// Copies `text` into `buf`, or its longest UTF-8-safe prefix plus a `…` marker
/// when it does not fit. The marker shares the buffer, so the result is always
/// at most `buf.len` bytes and never ends mid-codepoint.
fn fitWithEllipsis(text: []const u8, buf: []u8) []const u8 {
    if (text.len <= buf.len) {
        @memcpy(buf[0..text.len], text[0..text.len]);
        return buf[0..text.len];
    }
    var n: usize = buf.len - 3; // room for the 3-byte U+2026 ellipsis
    while (n > 0 and (text[n] & 0xC0) == 0x80) n -= 1; // do not split a codepoint
    @memcpy(buf[0..n], text[0..n]);
    buf[n] = 0xE2; // U+2026 HORIZONTAL ELLIPSIS, written byte-wise so the buffer
    buf[n + 1] = 0x80; // arithmetic stays explicit (no UTF-8 slicing tricks)
    buf[n + 2] = 0xA6;
    return buf[0..n + 3];
}

var g_failed_notes: [MAX_SUMMARY_FAILS]FailedTestNote = undefined;
var g_failed_ident: [MAX_SUMMARY_FAILS]NodeIdIdentity = undefined;
var g_failed_note_count: usize = 0;
var g_failed_nodeid_buf: [MAX_SUMMARY_FAILS][SUMMARY_NODEID_MAX]u8 = undefined;
var g_failed_reason_buf: [MAX_SUMMARY_FAILS][SUMMARY_REASON_MAX]u8 = undefined;

/// Failure records that arrived after the display ring filled up. Saturating so
/// a pathological run cannot wrap the counter; the printed wording calls them
/// *reports*, never "unique tests".
var g_failed_omitted_reports: u64 = 0;

/// Drops the records of a previous run (unit tests re-enter the frame parser).
pub fn resetFailedTestNotes() void {
    g_failed_note_count = 0;
    g_failed_omitted_reports = 0;
}

pub fn failedTestNoteCount() usize {
    return g_failed_note_count;
}

/// Failure reports that did not fit the display ring. A repeated report of an
/// unlisted failure counts again — this is a record count, not a unique-test
/// count, and the printed line says so.
pub fn failedTestOmittedReports() u64 {
    return g_failed_omitted_reports;
}

pub fn failedTestNotes() []const FailedTestNote {
    return g_failed_notes[0..g_failed_note_count];
}

fn isDecorativeRule(line: []const u8) bool {
    if (line.len == 0) return true;
    for (line) |ch| {
        if (ch != '-' and ch != '=' and ch != '_') return false;
    }
    return true;
}

/// Last non-empty, non-decorative line of a traceback/failure report. For a
/// Python traceback that is the `SomeError: message` line pytest itself puts
/// in its short summary; the `_____ test_name _____` headers of a failure
/// report are rules and never qualify.
fn lastMeaningfulLine(text: []const u8) ?[]const u8 {
    var it = std.mem.splitScalar(u8, text, '\n');
    var found: ?[]const u8 = null;
    while (it.next()) |raw| {
        const ln = std.mem.trimEnd(u8, raw, " \t\r");
        if (isDecorativeRule(ln)) continue;
        found = std.mem.trimStart(u8, ln, " \t");
    }
    return found;
}

/// First line of `text` that opens with the exception class name — for a Python
/// traceback that is the `AssertionError: assert 1 == 2` line, even when the
/// exception message itself spans several lines (then the LAST line of the
/// traceback is just a continuation like `Right: 2` and must not be picked).
fn namedErrorLine(text: []const u8, err_name: []const u8) ?[]const u8 {
    if (err_name.len == 0) return null;
    var it = std.mem.splitScalar(u8, text, '\n');
    while (it.next()) |raw| {
        var ln = std.mem.trim(u8, raw, " \t\r");
        // pytest's exception-report lines carry an "E   " gutter.
        if (ln.len > 2 and ln[0] == 'E' and ln[1] == ' ') ln = std.mem.trimStart(u8, ln[1..], " ");
        if (ln.len != 0 and std.mem.startsWith(u8, ln, err_name)) return ln;
    }
    return null;
}

/// One-line reason for the short summary block: the `Error: message` line when
/// the failure text has one, else the last meaningful line, else the class name.
pub fn shortFailReason(err_name: []const u8, msg: []const u8, tb: []const u8) []const u8 {
    if (namedErrorLine(tb, err_name)) |ln| return ln;
    if (namedErrorLine(msg, err_name)) |ln| return ln;
    if (lastMeaningfulLine(tb)) |ln| return ln;
    if (lastMeaningfulLine(msg)) |ln| return ln;
    if (err_name.len != 0) return err_name;
    return "failed";
}

/// Records one failed test. The dedup key is the 128-bit identity of the FULL
/// nodeid, so a failure re-reported by a teardown phase never yields two lines
/// while two distinct long nodeids that share a display prefix stay distinct.
pub fn recordFailedTest(nodeid: []const u8, reason: []const u8) void {
    if (nodeid.len == 0) return;
    const ident = nodeIdIdentity(nodeid);
    for (g_failed_ident[0..g_failed_note_count]) |seen| {
        if (identityEq(seen, ident)) return;
    }
    if (g_failed_note_count >= MAX_SUMMARY_FAILS) {
        // Display ring full: count the record instead of dropping it silently.
        // A repeat of a *listed* nodeid was already collapsed above; a repeat of
        // an unlisted one counts again, because proving uniqueness past the ring
        // needs unbounded storage. The printed line therefore says "reports".
        g_failed_omitted_reports +|= 1;
        return;
    }
    const slot = g_failed_note_count;
    const nid_view = fitWithEllipsis(nodeid, g_failed_nodeid_buf[slot][0..]);
    const trimmed = std.mem.trim(u8, reason, " \t\r");
    const why_view = fitWithEllipsis(trimmed, g_failed_reason_buf[slot][0..]);
    g_failed_notes[slot] = .{
        .nodeid = nid_view,
        .reason = why_view,
    };
    g_failed_ident[slot] = ident;
    g_failed_note_count += 1;
}

/// The `=== short test summary info ===` block. Not printed at all when no test
/// failed — a clean run keeps its one-line summary. When the display ring is
/// full, a trailing line states how many failures are not listed, so a partial
/// block cannot be mistaken for a complete one.
pub fn printShortTestSummary() void {
    if (g_failed_note_count == 0 and g_failed_omitted_reports == 0) return;
    writeStdout("=== short test summary info ===\n");
    var line_buf: [1024]u8 = undefined;
    for (g_failed_notes[0..g_failed_note_count]) |note| {
        const line = std.fmt.bufPrint(
            &line_buf,
            "{s}FAILED{s} {s} - {s}\n",
            .{ cRed(), cReset(), note.nodeid, note.reason },
        ) catch continue;
        writeStdout(line);
    }
    if (g_failed_omitted_reports != 0) {
        var tail_buf: [160]u8 = undefined;
        if (omittedSummaryLine(&tail_buf)) |tail| writeStdout(tail);
    }
}

/// The line that makes a truncated summary honest: how many failure records are
/// not listed. Deliberately not phrased as a count of unique tests — the ring
/// cannot know that, and a wrong "at least N" would be worse than no number.
fn omittedSummaryLine(buf: []u8) ?[]const u8 {
    const line: ?[]const u8 = std.fmt.bufPrint(
        buf,
        "... {d} more failed-test reports not shown (summary lists the first {d})\n",
        .{ g_failed_omitted_reports, g_failed_note_count },
    ) catch null;
    return line;
}

/// Parses one worker frame and applies it to the summary.
/// Returns the frame's progress classification so the caller can re-arm the per-test inactivity
/// deadline with the right budget (heartbeat semantics:
/// TEST_START/TEST_END/TEARDOWN_START are pure heartbeat frames — they never mutate the summary).
pub fn parseAndApplyFrameJson(
    a: std.mem.Allocator,
    io: ?std.Io,
    payload: []const u8,
    summary: *FileRunSummary,
    llm_mode: bool,
    verbose: bool,
    collect_only: bool,
) IpcError!FrameProgress {
    var parsed = std.json.parseFromSlice(std.json.Value, a, payload, .{}) catch return error.MalformedJsonPayload;
    defer parsed.deinit();

    const root = switch (parsed.value) {
        .object => |o| o,
        else => return error.MalformedJsonPayload,
    };

    const ev_type = jsonGetString(root, "t", "");
    if (ev_type.len == 0) return error.MalformedJsonPayload;

    if (std.mem.eql(u8, ev_type, "COLLECT_ITEM")) {
        if (collect_only) {
            if (io != null) {
                const nid = jsonGetString(root, "nodeid", "");
                if (std.fmt.allocPrint(a, "{s}\n", .{nid}) catch null) |line| writeStdout(line);
            }
        }
    } else if (std.mem.eql(u8, ev_type, "COLLECT_BATCH")) {
        if (collect_only) {
            if (io != null) {
                if (root.get("nodeids")) |arr_val| {
                    switch (arr_val) {
                        .array => |arr| {
                            for (arr.items) |it_val| {
                                switch (it_val) {
                                    .string => |s| {
                                        if (std.fmt.allocPrint(a, "{s}\n", .{s}) catch null) |line| writeStdout(line);
                                    },
                                    else => {},
                                }
                            }
                        },
                        else => {},
                    }
                }
            }
        }
    } else if (std.mem.eql(u8, ev_type, "TEST_PASS")) {
        summary.frame_passed +%= 1;
        if (!llm_mode and verbose) {
            if (io != null) {
                const nid = jsonGetString(root, "nodeid", "");
                const dur = jsonGetU32(root, "duration_ms") catch 0;
                if (std.fmt.allocPrint(a, "  {s}✓{s} {s} ({d}ms)\n", .{ cGreen(), cReset(), nid, dur }) catch null) |line| writeStdout(line);
            }
        }
    } else if (std.mem.eql(u8, ev_type, "TEST_SKIP")) {
        summary.frame_skipped +%= 1;
        if (!llm_mode and verbose) {
            if (io != null) {
                const nid = jsonGetString(root, "nodeid", "");
                if (std.fmt.allocPrint(a, "  {s}-{s} {s} (skipped)\n", .{ cYellow(), cReset(), nid }) catch null) |line| writeStdout(line);
            }
        }
    } else if (std.mem.eql(u8, ev_type, "TEST_FAIL")) {
        summary.frame_failed +%= 1;
        if (io != null) {
            const nid = jsonGetString(root, "nodeid", "");
            const loc = jsonGetString(root, "loc", "");
            const err_name = jsonGetString(root, "error", "AssertionError");
            const msg = jsonGetString(root, "msg", "");
            const tb = jsonGetString(root, "tb", "");
            // Human mode: remember the failure for the `=== short test summary
            // info ===` block. The reason is one line by construction, so the
            // block stays a scannable index of what broke.
            if (!llm_mode) recordFailedTest(nid, shortFailReason(err_name, msg, tb));
            if (llm_mode) {
                if (std.fmt.allocPrint(
                    a,
                    "{{\"t\":\"fail\",\"id\":{f},\"loc\":{f},\"error\":{f},\"msg\":{f}}}\n",
                    .{ std.json.fmt(nid, .{}), std.json.fmt(loc, .{}), std.json.fmt(err_name, .{}), std.json.fmt(msg, .{}) },
                ) catch null) |line| writeStdout(line);
            } else {
                if (std.fmt.allocPrint(
                    a,
                    "\n{s}FAIL{s} {s} ({s})\n{s}\n",
                    .{ cRed(), cReset(), nid, loc, tb },
                ) catch null) |line| writeStdout(line);
            }
        }
    } else if (std.mem.eql(u8, ev_type, "FILE_DONE")) {
        summary.passed = try jsonGetU32(root, "passed");
        summary.failed = try jsonGetU32(root, "failed");
        summary.skipped = try jsonGetU32(root, "skipped");
        summary.deselected = try jsonGetU32(root, "deselected");
        // Collect-only FILE_DONE frames carry the session-level
        // collected count. An executing-mode FILE_DONE carries no field: the
        // count from COLLECTION_DONE stays authoritative instead of being
        // wiped to 0 (jsonGetU32 maps an absent field to 0).
        if (root.get("collected") != null) summary.collected = try jsonGetU32(root, "collected");
        const st = jsonGetString(root, "status", "UNKNOWN");
        const copy_len = @min(st.len, summary.status_buf.len);
        @memcpy(summary.status_buf[0..copy_len], st[0..copy_len]);
        summary.status_len = copy_len;
        summary.got_file_done = true;
    } else if (std.mem.eql(u8, ev_type, "COLLECTION_DONE")) {
        // The collection window closes here and the worker reports how many
        // items survived selection. Kept as the file's collected count so a
        // worker that dies before FILE_DONE still reports its real size.
        if (root.get("collected") != null) summary.collected = try jsonGetU32(root, "collected");
    }

    // Per-test inactivity re-arm set: only verified progress frames
    // extend the deadline. TEST_START/TEST_END/TEARDOWN_START are pure heartbeat frames (they carry no
    // summary semantics) that close the setup/teardown windows so inactivity billing is per-test.
    // COLLECTION_DONE closes the import/collection window before setup_module: the collection
    // itself stays under the FILE_START budget, while a hung setup_module is now billed against the
    // ordinary per-test --timeout budget instead of hiding inside the 10-minute collection window.
    if (std.mem.eql(u8, ev_type, "FILE_START")) return .file_start;
    return if (std.mem.eql(u8, ev_type, "TEST_PASS") or
        std.mem.eql(u8, ev_type, "TEST_SKIP") or
        std.mem.eql(u8, ev_type, "TEST_FAIL") or
        std.mem.eql(u8, ev_type, "TEST_START") or
        std.mem.eql(u8, ev_type, "TEST_END") or
        std.mem.eql(u8, ev_type, "TEARDOWN_START") or
        std.mem.eql(u8, ev_type, "PHASE_START") or
        std.mem.eql(u8, ev_type, "COLLECT_BATCH") or
        std.mem.eql(u8, ev_type, "COLLECTION_DONE")) FrameProgress.progress else FrameProgress.none;
}

/// Active worker slot with incremental non-blocking frame-automaton state.
pub const ActiveWorker = struct {
    file_path: []const u8,
    worker_pid: i32,
    conn_fd: linux.fd_t,
    used_exec_mode: bool,
    expected_seq: u32 = 0,
    deadline_ms: i64 = 0,
    /// Accept deadline of this spawn, armed at spawn time (`now + 10 s`):
    /// a refilled slot is judged on its own connection window and never
    /// inherits a predecessor's (expired) budget.
    accept_deadline_ms: i64 = 0,
    eof: bool = false,
    /// Post-FILE_DONE exit deadline: a clean-EOF worker that lingers in its exit
    /// path (e.g. a test monkeypatched os._exit) is SIGKILLed (group + leader) 3 s after its IPC
    /// stream ended, inside the same poll iteration — instead of riding along until the last
    /// sibling finishes. 0 = no deadline armed (already killed, or never had FILE_DONE).
    exit_deadline_ms: i64 = 0,
    /// pidfd of the worker, opened at accept time: POLLIN fires the moment the
    /// worker exits (zombie included), so an eof slot's exit deadline is discharged instantly
    /// instead of riding the full 3 s grace. -1 = unavailable (degrade to deadline-only).
    worker_pidfd: linux.fd_t = -1,
    /// REAP ownership discipline: set the instant a `REAP` request has been
    /// written to the Zygote and cleared only when the matching `REAPED\t<pid>\t<status>` answer
    /// has been parsed. The Zygote `waitpid()`s the worker before it answers, so inside this
    /// window the numeric pid may already have been recycled by an unrelated process of the same
    /// uid — the slot must not signal it again: no `kill(pid)`, no `kill(-pid)` group sweep, no
    /// pid-based reap, no watchdog-registry (numeric pgid) kill. Whatever is still out there
    /// passes to the group/pidfd paths and to the final containment sweep, which only ever
    /// touches processes that are our own children (adopted via PR_SET_CHILD_SUBREAPER) and
    /// therefore can never be a recycled pid.
    reap_unconfirmed: bool = false,
    summary: FileRunSummary = .{},
    hdr_buf: [IPC_HEADER_SIZE]u8 = @splat(0),
    hdr_off: usize = 0,
    payload_buf: []u8 = &.{},
    payload_off: usize = 0,
};

/// State of a run-scoped dispatch slot.
/// `empty` slots carry no live state at all — their `aw` is undefined garbage and is never
/// read; `active` slots are supervised by the rolling poll loop.
pub const SlotState = enum { empty, active };

/// Run-scoped slot of the rolling slot-refill dispatcher. One slot per `-j` worker; a
/// slot is reused across files of the run (socket path + listen fd are stable per slot, the
/// worker identity is re-committed by the full `ActiveWorker` literal at every refill).
pub const RunSlot = struct {
    state: SlotState = .empty,
    aw: ActiveWorker = undefined,
};

/// Terminal transition for a slot: close the stream, drop partial-frame state, and — when the stream
/// ended (or errored) without FILE_DONE — SIGKILL the worker and its group immediately: a
/// worker that relinquishes IPC must not keep running past its own timeout budget.
/// A clean post-FILE_DONE exiter gets a 3 s exit deadline enforced by the poll loop;
/// its exit is additionally bounded by the Phase 4 reap deadline.
/// Safe on pinned zombies (kill on a dead group leader is a harmless no-op for it, still sweeps live
/// group members) and on already-dead pids (ESRCH).
fn finalizeSlotEof(gpa: std.mem.Allocator, aw: *ActiveWorker) void {
    aw.eof = true;
    if (aw.conn_fd >= 0) {
        _ = linux.close(aw.conn_fd);
        aw.conn_fd = -1;
    }
    if (aw.payload_buf.len > 0) {
        gpa.free(aw.payload_buf);
        aw.payload_buf = &.{};
    }
    if (!aw.summary.got_file_done and aw.worker_pid > 0) {
        killGroup(aw.worker_pid);
        _ = linux.kill(aw.worker_pid, linux.SIG.KILL);
        aw.exit_deadline_ms = 0;
    } else if (aw.summary.got_file_done and aw.worker_pid > 0) {
        // Clean EOF: bound the lingering exiter. The pid stays pinned as a zombie until
        // Phase 4, so a later kill(-pid) can never hit a reused pid.
        aw.exit_deadline_ms = monotonicMs() + 3_000;
    }
}

/// Truly non-blocking incremental frame automaton: drains available bytes from a ready worker socket
/// processing each complete frame as soon as it assembles. Never blocks waiting for more bytes inside
/// one worker (head-of-line blocking). Bounded by MAX_FRAMES_PER_DRAIN and an
/// unconditional isInterrupted() check per iteration so an always-readable flooding worker can neither
/// monopolize the scheduler nor delay signal handling (fairness).
/// Re-arms `aw.deadline_ms` on every verified progress frame; FILE_START
/// re-arms with the bounded collection budget max(timeout, 600 s).
/// Returns `false` on clean EOF exactly at a frame boundary; `true` when drained to EAGAIN or the
/// frame budget is exhausted (poll is level-triggered: remaining data is serviced next iteration).
const MAX_FRAMES_PER_DRAIN: usize = 64;
fn drainWorkerFrames(
    parse_arena: *std.heap.ArenaAllocator,
    gpa: std.mem.Allocator,
    io: std.Io,
    aw: *ActiveWorker,
    worker_read_timeout_ms: i64,
    llm_mode: bool,
    verbose: bool,
    collect_only: bool,
) IpcError!bool {
    var frames_this_drain: usize = 0;
    while (true) {
        if (isInterrupted()) return error.Interrupted;
        if (aw.hdr_off < IPC_HEADER_SIZE) {
            const rc = linux.read(aw.conn_fd, aw.hdr_buf[aw.hdr_off..].ptr, IPC_HEADER_SIZE - aw.hdr_off);
            switch (linux.errno(rc)) {
                .SUCCESS => {
                    if (rc == 0) {
                        if (aw.hdr_off == 0) return false; // clean EOF at frame boundary
                        return error.UnexpectedEof;
                    }
                    aw.hdr_off += rc;
                    if (aw.hdr_off < IPC_HEADER_SIZE) continue;
                    const payload_len = std.mem.readInt(u32, aw.hdr_buf[12..16], .little);
                    if (payload_len > 16 * 1024 * 1024) return error.PayloadTooLarge;
                    if (payload_len > 0) {
                        aw.payload_buf = gpa.alloc(u8, payload_len) catch return error.ReadFailed;
                    }
                    aw.payload_off = 0;
                },
                .INTR => {
                    if (isInterrupted()) return error.Interrupted;
                    continue;
                },
                .AGAIN => return true, // socket buffer drained; return to the multiplexing poll loop
                else => return error.ReadFailed,
            }
            continue;
        }

        if (aw.payload_off < aw.payload_buf.len) {
            const rc = linux.read(aw.conn_fd, aw.payload_buf[aw.payload_off..].ptr, aw.payload_buf.len - aw.payload_off);
            switch (linux.errno(rc)) {
                .SUCCESS => {
                    if (rc == 0) return error.UnexpectedEof;
                    aw.payload_off += rc;
                },
                .INTR => {
                    if (isInterrupted()) return error.Interrupted;
                    continue;
                },
                .AGAIN => return true,
                else => return error.ReadFailed,
            }
            if (aw.payload_off < aw.payload_buf.len) continue;
        }

        // Full frame assembled: verify CRC/seq/pid, apply, and re-arm the inactivity deadline on progress.
        _ = try decodeAndVerifyFrame(&aw.hdr_buf, aw.payload_buf, aw.expected_seq, aw.worker_pid);
        // Sequence numbers use a wrapping add; the shim mirrors it (& 0xFFFFFFFF) —
        // a saturating add would pin at maxInt(u32) and cause a permanent SequenceMismatch.
        aw.expected_seq +%= 1;
        _ = parse_arena.reset(.retain_capacity);
        const prog = try parseAndApplyFrameJson(parse_arena.allocator(), io, aw.payload_buf, &aw.summary, llm_mode, verbose, collect_only);
        if (prog != .none) {
            aw.deadline_ms = monotonicMs() + frameBudgetMs(worker_read_timeout_ms, prog);
        }
        if (aw.payload_buf.len > 0) {
            gpa.free(aw.payload_buf);
            aw.payload_buf = &.{};
        }
        aw.hdr_off = 0;
        aw.payload_off = 0;
        frames_this_drain += 1;
        if (frames_this_drain >= MAX_FRAMES_PER_DRAIN) return true; // fairness budget: back to poll()
    }
}

/// Bounded WNOHANG reap loop: never blocks forever on a D-state worker.
/// Returns the raw wait status, or -1 if the child could not be reaped within `deadline_ms`.
fn reapChildBounded(pid: i32, deadline_ms: i64) i32 {
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
        if (remainingMs(deadline_ms) <= 0) return -1;
        var req_ts: linux.timespec = .{ .sec = 0, .nsec = 2_000_000 };
        _ = linux.nanosleep(&req_ts, null);
    }
}

/// Non-blocking wait4 loop with deadline + WNOWAIT group sweep.
/// Interrupt-aware: on SIGINT/SIGTERM it SIGKILLs the worker group and reaps with a bound instead of
/// waiting out the full deadline.
fn waitPidWithDeadline(pid: i32, deadline_ms: i64) i32 {
    var siginfo_buf: [128]u8 align(8) = @splat(0);
    while (remainingMs(deadline_ms) > 0) {
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

/// REAP ownership: the slot has sent `REAP` and will never
/// learn whether the Zygote reaped the child. Drop every numeric-pid privilege of the slot:
///   * `reap_unconfirmed` makes the run-scoped cleanup defer skip kill/killGroup/reap-by-pid,
///   * the ADR-004 watchdog registry entry is released, because that layer kills by numeric
///     `pgid` on supervisor death — a recycled pgid would be a foreign group,
///   * a one-line diagnostic is emitted so the give-up is observable (never silent).
/// Ownership of anything still running passes to the pidfd path (already closed), the worker's
/// process group, and the final containment sweep, which enumerates our own adopted children and
/// therefore cannot match a recycled pid.
fn relinquishPidOwnership(aw: *ActiveWorker, reason: []const u8) void {
    // `reap_unconfirmed` is already set by the finalize (it is armed by the REAP write); both the
    // flag and PgidRegistry.unregister are idempotent, so re-entry is harmless.
    aw.reap_unconfirmed = true;
    if (aw.worker_pid > 0 and g_wd_reg.slots.len > 0) {
        g_wd_reg.unregister(aw.worker_pid);
    }
    var msg_buf: [192]u8 = undefined;
    const msg = std.fmt.bufPrint(&msg_buf, "ztest: REAP ownership released for pid {d} ({s}); no numeric signal will be sent to it\n", .{ aw.worker_pid, reason }) catch return;
    writeStderr(msg);
}

/// Early-exit / interrupt cleanup of one dispatcher slot. The ownership rule:
///   * unconditional, ownership-free work always runs: close conn fd, close pidfd, free payload;
///   * if `reap_unconfirmed` is set (a `REAP` request was handed to the Zygote and no matching
///     `REAPED\t<pid>\t<status>` answer came back) nothing numeric is signaled on the pid: no
///     leader kill, no group kill, no pid-based reap, and no ADR-004 unregister either — that
///     layer kills by numeric pgid on supervisor death, so a recycled pgid would be a foreign
///     group. The pid stays in `protected_pids` as a sweep EXCLUDE for the rest of the run;
///     containment of whatever still runs passes to the Zygote teardown and to the final
///     containment sweep, which enumerates our own adopted children (a recycled pid can never be
///     one of them).
///   * `worker_pid` is zeroed in both cases, so no later phase can signal it by number either.
fn abortSlotCleanup(
    gpa: std.mem.Allocator,
    aw: *ActiveWorker,
    zygote_worker_pids: *std.ArrayListUnmanaged(i32),
    protected: *std.ArrayListUnmanaged(i32),
) void {
    if (aw.payload_buf.len > 0) {
        gpa.free(aw.payload_buf);
        aw.payload_buf = &.{};
    }
    if (aw.conn_fd >= 0) {
        _ = linux.close(aw.conn_fd);
        aw.conn_fd = -1;
    }
    if (aw.worker_pidfd >= 0) {
        _ = linux.close(aw.worker_pidfd);
        aw.worker_pidfd = -1;
    }
    if (aw.worker_pid > 0) {
        if (!aw.reap_unconfirmed) {
            killGroup(aw.worker_pid);
            _ = linux.kill(aw.worker_pid, linux.SIG.KILL);
            // ADR-004 layer 3: unregister before reap — the same ordering as the
            // regular Phase 4: the watchdog-registry slot is released before
            // the reap can hand the pid back for reuse.
            if (g_wd_reg.slots.len > 0) {
                g_wd_reg.unregister(aw.worker_pid);
            }
            if (aw.used_exec_mode) {
                // Exec-mode workers are our direct children: reap to avoid zombie leakage.
                _ = reapChildBounded(aw.worker_pid, monotonicMs() + 1_000);
            } else {
                // Zygote workers stay registered so a later Zygote teardown reaps them.
                removePidFromList(zygote_worker_pids, aw.worker_pid);
            }
            removePidFromList(protected, aw.worker_pid);
        }
        aw.worker_pid = 0;
    }
}

pub fn main(init: std.process.Init) u8 {
    g_use_color = stdoutIsTty() and init.environ_map.get("NO_COLOR") == null;
    const rc = runMain(init) catch |err| {
        if (err == error.Interrupted) {
            // A Ctrl-C must not be a silent exit: report the partial tallies of
            // the files that did finish, then keep pytest's interrupted status.
            printInterruptSummary();
            outFlushAll();
            return ExitCode.interrupted.toU8();
        }
        if (err == error.UsageError) {
            outFlushAll();
            return ExitCode.usage_error.toU8();
        }
        const msg = std.fmt.allocPrint(init.gpa, "ztest internal error: {s}\n", .{@errorName(err)}) catch "ztest internal error\n";
        writeStderr(msg);
        outFlushAll();
        return ExitCode.internal_error.toU8();
    };
    outFlushAll();
    if (rc == 0 and (g_out_stdout.dropped_bytes > 0 or g_out_stdout.write_err)) {
        return ExitCode.interrupted.toU8();
    }
    return rc;
}

fn removePidFromList(list: *std.ArrayListUnmanaged(i32), pid: i32) void {
    for (list.items, 0..) |p, i| {
        if (p == pid) {
            _ = list.orderedRemove(i);
            return;
        }
    }
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
fn killAdoptedOrphans(
    self_pid: i32,
    exclude: []const i32,
    protected_groups: []const i32,
    older_than_ticks: ?u64,
) void {
    const budget_deadline: i64 = monotonicMs() + 10_000;
    // direct-children primary path: O(children) through /proc/self/task/<pid>/children.
    var children_buf: [8192]u8 = undefined;
    if (sweepAdoptedViaProcChildren(self_pid, exclude, protected_groups, budget_deadline, older_than_ticks, &children_buf)) return;

    // Fallback (direct-children): full /proc scan.
    const open_rc = linux.open("/proc", .{ .ACCMODE = .RDONLY, .DIRECTORY = true, .CLOEXEC = true }, 0);
    if (linux.errno(open_rc) != .SUCCESS) return;
    const dir_fd: linux.fd_t = @intCast(open_rc);
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
            sweepOneAdoptee(pid, self_pid, exclude, protected_groups, budget_deadline, older_than_ticks);
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
    buf: []u8,
) bool {
    const thread_count = countProcTasks(self_pid) catch return false;
    if (thread_count != 1) return false;

    var path_buf: [64]u8 = @splat(0);
    const path = std.fmt.bufPrintSentinel(&path_buf, "/proc/self/task/{d}/children", .{self_pid}, 0) catch return false;
    const fd_rc = linux.open(path.ptr, .{ .ACCMODE = .RDONLY, .CLOEXEC = true }, 0);
    if (linux.errno(fd_rc) != .SUCCESS) return false;
    const fd: linux.fd_t = @intCast(fd_rc);
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
        sweepOneAdoptee(pid, self_pid, exclude, protected_groups, budget_deadline, older_than_ticks);
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
) void {
    if (pid <= 0 or pid == self_pid) return;
    if (pid == g_wd_pid) return; // the run watchdog is untouchable here (spared by every sweep)
    if (pidInList(exclude, pid)) return;

    // Parse /proc/<pid>/stat: fields after the last ')' are: state(1) ppid(2) pgrp(3)...
    var stat_path_buf: [64]u8 = @splat(0);
    const stat_path = std.fmt.bufPrintSentinel(&stat_path_buf, "/proc/{d}/stat", .{pid}, 0) catch return;
    const sfd_rc = linux.open(stat_path.ptr, .{ .ACCMODE = .RDONLY, .CLOEXEC = true }, 0);
    // Race: a pid listed in `children` may already be gone by the time we stat it (reaped
    // elsewhere) -> ENOENT -> skip, exactly the degradation the full scan always had.
    if (linux.errno(sfd_rc) != .SUCCESS) return;
    const sfd: linux.fd_t = @intCast(sfd_rc);
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
        _ = waitPidWithDeadline(pid, wait_deadline);
    }
}

/// `starttime` of a process from an already READ /proc/<pid>/stat buffer.
///
/// Field layout (man 5 proc), 1-based: 1 pid, 2 comm, 3 state, 4 ppid, 5 pgrp, 6 session,
/// 7 tty_nr, 8 tpgid, 9 flags, 10 minflt, 11 cminflt, 12 majflt, 13 cmajflt, 14 utime,
/// 15 stime, 16 cutime, 17 cstime, 18 priority, 19 nice, 20 num_threads, 21 itrealvalue,
/// 22 starttime. Fields 3.. are positional only after the last ')' (comm may contain spaces and
/// ')'), so the first token after the last ')' is field 3 (state) and starttime — field 22 —
/// is token index 22 - 3 = 19. Nineteen tokens are therefore skipped before the read.
fn parseStartTimeTicks(stat_slice: []const u8) ?u64 {
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
fn procStartTimeTicks(pid: i32) ?u64 {
    if (pid <= 0) return null;
    var path_buf: [64]u8 = @splat(0);
    const path = std.fmt.bufPrintSentinel(&path_buf, "/proc/{d}/stat", .{pid}, 0) catch return null;
    const fd_rc = linux.open(path.ptr, .{ .ACCMODE = .RDONLY, .CLOEXEC = true }, 0);
    if (linux.errno(fd_rc) != .SUCCESS) return null;
    const fd: linux.fd_t = @intCast(fd_rc);
    var stat_buf: [512]u8 = @splat(0);
    const rd_rc = linux.read(fd, &stat_buf, stat_buf.len);
    _ = linux.close(fd);
    if (linux.errno(rd_rc) != .SUCCESS or rd_rc <= 0) return null;
    return parseStartTimeTicks(stat_buf[0..@intCast(rd_rc)]);
}

/// The orphan-sweep ownership threshold — `min` starttime over every live worker of the
/// rolling dispatcher (`state == .active` and a committed pid). A candidate strictly older than
/// this value cannot be a descendant of any live worker.
///
/// * `null` — no live worker at all: no live sibling's domain to protect, kill everything unprotected.
/// * `0`    — some live worker's starttime could not be read. Zero is never greater than any real
///            starttime, so every candidate hits the `>= threshold` guard and is spared: the
///            sweep degrades to "kill nothing", which is the safe answer (a too-large
///            threshold would kill a live sibling's descendant).
fn minActiveWorkerStartTicks(slots: []const RunSlot) ?u64 {
    var min_ticks: ?u64 = null;
    var unknown = false;
    for (slots) |*s| {
        if (s.state != .active or s.aw.worker_pid <= 0) continue;
        const ticks = procStartTimeTicks(s.aw.worker_pid) orelse {
            unknown = true;
            continue;
        };
        if (min_ticks == null or ticks < min_ticks.?) min_ticks = ticks;
    }
    if (unknown) return 0;
    return min_ticks;
}

/// FAIL-CLOSED group kill: kill(-pgid) with pgid == 1 is kill(-1) — a broadcast
/// SIGKILL to the whole uid; pgid == 0 is kill(0) — our own group. Neither form
/// is ever a legal containment target of ztest (a worker group is always
/// pgid == pid > 1). The refusal is silent and cheap: an ESRCH equivalent.
fn killGroup(pgid: i32) void {
    if (pgid <= 1) return;
    _ = linux.kill(-pgid, linux.SIG.KILL);
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
///  - on the writer's death: killAll(g_wd_reg) — killpg(-pgid, SIGKILL) per entry — _exit(0).
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

/// Global view of the mmap registry: after spawnBatchWatchdog its copy in the watchdog refers to
/// the same shared anonymous mapping; the supervisor writes through its own view. An empty view in
/// the watchdog is impossible: spawn installs it before first use.
var g_wd_reg: PgidRegistry.View = .{ .slots = &.{} };

/// Pid of the live run watchdog (0 = no watchdog). Every adopted-children sweep must
/// spare the watchdog: otherwise the very first per-slot sweep
/// or killAndCloseZygote would reap it (ppid == supervisor, own pgrp), and the final
/// defer would then fire SIGKILL at an already-reaped (recycled) pid.
var g_wd_pid: i32 = 0;

fn spawnBatchWatchdog(io: std.Io) ?i32 {
    _ = io;
    const sup_pidfd = pidfdOpen(@intCast(linux.getpid()));
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
    _ = linux.close_range(@as(linux.fd_t, @intCast(sup_pidfd + 1)), std.math.maxInt(linux.fd_t), .{ .CLOEXEC = false, .UNSHARE = false });
    if (sup_pidfd > 3) {
        _ = linux.close_range(3, @as(linux.fd_t, @intCast(sup_pidfd - 1)), .{ .CLOEXEC = false, .UNSHARE = false });
    }
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
    // This view is the shared MAP_SHARED|MAP_ANONYMOUS mapping; its pages are not in real_addr if g_wd_reg is empty.
    g_wd_reg.killAll();
    linux.exit_group(0);
}

/// Zygote teardown: closes the control
/// channel and kills+reaps only the Zygote itself. Registered Zygote workers are not killed by this
/// function directly — the ADR-004 Layer 1 cascade (the worker re-arms PR_SET_PDEATHSIG onto
/// the zygote post-fork) makes the kernel finish off in-flight workers too: their slots
/// finalize on EOF without FILE_DONE → CRASHED. killAndCloseZygote
/// is called only on a hard zygote desync (a corrupted python image), and workers forked off
/// it are untrusted; a rerun beats a false PASS. ADR-004 documents the containment model.
/// The /proc scan then kills only never-registered adoptees (FORK-race children, escaped descendants);
/// Watchdog processes of live protected workers (same pgrp, adopted by us) survive.
fn killAndCloseZygote(
    zygote_conn_fd: *?linux.fd_t,
    zygote_pid: *i32,
    protected: []const i32,
) void {
    if (zygote_conn_fd.*) |zfd| {
        _ = linux.close(zfd);
        zygote_conn_fd.* = null;
    }

    // Kill the Zygote itself (+ its own group only if it verified as a group leader at accept time).
    if (zygote_pid.* > 0) {
        const zpg_rc = linux.syscall1(.getpgid, @as(usize, @intCast(zygote_pid.*)));
        if (linux.errno(zpg_rc) == .SUCCESS and zpg_rc == @as(usize, @intCast(zygote_pid.*))) {
            killGroup(zygote_pid.*);
        }
        _ = linux.kill(zygote_pid.*, linux.SIG.KILL);
        _ = waitPidWithDeadline(zygote_pid.*, monotonicMs() + 3_000);
        zygote_pid.* = 0;
    }

    // /proc sweep for never-registered adoptees only (FORK-timeout race children, setsid-escaped
    // descendants): registered workers (protected) keep their ownership chain through Phase 3/4 —
    // and their double-forked watchdogs (living in the worker's pgrp) survive too.
    const self_pid: i32 = @intCast(linux.getpid());
    // No starttime threshold here — this is the Zygote teardown path (desync / hard IPC
    // error), where the Zygote's whole worker tree is already condemned by its pdeathsig
    // cascade; the sweep is the last containment net for that tree. Rolling-path sweeps use
    // `minActiveWorkerStartTicks` instead.
    killAdoptedOrphans(self_pid, protected, protected, null);

    // Final selective zombie sweep for anything already reaped-able (zombies adopted at the Zygote's
    // death). Protected (registered) worker zombies stay pinned so active slots never signal a
    // recycled pid. Statuses are not recorded.
    reapAdoptedChildren(protected);
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
/// clobber concurrent writers; `initOutputChannel` performs that classification via fstat first.
fn openPrivateOutputFd(std_fd: i32) ?linux.fd_t {
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
fn statFileSize(path_z: [*:0]const u8) u64 {
    var stx: linux.Statx = undefined;
    const rc = linux.statx(linux.AT.FDCWD, path_z, 0, .{ .SIZE = true }, &stx);
    if (linux.errno(rc) != .SUCCESS) return 0;
    return stx.size;
}

/// Architecture-independent statx(2) on an open fd.
/// Returns the stx_mode bits, or null on failure (caller degrades conservatively).
fn fstatMode(fd: linux.fd_t) ?u32 {
    var stx: linux.Statx = undefined;
    const rc = linux.statx(fd, "", linux.AT.EMPTY_PATH, .{ .TYPE = true, .MODE = true }, &stx);
    if (linux.errno(rc) != .SUCCESS) return null;
    return stx.mode;
}

/// statx(2) mode bits for a path, or null when the path is absent or cannot be stat'ed.
/// Symlinks are followed, which is the existence test a runner must apply to its arguments.
fn statModeBits(path: []const u8) ?u32 {
    var buf: [4096]u8 = undefined;
    const pz = std.fmt.bufPrintSentinel(&buf, "{s}", .{path}, 0) catch return null;
    var stx: linux.Statx = undefined;
    const rc = linux.statx(linux.AT.FDCWD, pz.ptr, 0, .{ .TYPE = true, .MODE = true }, &stx);
    if (linux.errno(rc) != .SUCCESS) return null;
    return stx.mode;
}

pub fn pathExists(path: []const u8) bool {
    return statModeBits(path) != null;
}

/// A usable interpreter path: an existing regular file with at least one execute bit set.
pub fn isExecutableFile(path: []const u8) bool {
    const mode_bits = statModeBits(path) orelse return false;
    if (!linux.S.ISREG(mode_bits)) return false;
    return (mode_bits & 0o111) != 0;
}

/// Strips a pytest `path::nodeid` selection down to the path it selects from.
pub fn targetBasePath(target: []const u8) []const u8 {
    if (std.mem.indexOf(u8, target, "::")) |i| return target[0..i];
    return target;
}

/// First explicitly named target that is absent from disk, or null when all of them exist.
/// A missing target is a usage error: starting a worker on it would only produce a file-level
/// collection crash wrapped in a Python traceback.
pub fn findMissingTarget(targets: []const []const u8) ?[]const u8 {
    for (targets) |t| {
        if (t.len == 0) continue;
        if (!pathExists(targetBasePath(t))) return t;
    }
    return null;
}

/// Resolves an explicit interpreter argument the way the spawn path does: a value containing a
/// separator must be an executable file, a bare command name is looked up in PATH.
pub fn interpreterAvailable(path: []const u8, path_env: ?[]const u8) bool {
    if (path.len == 0) return false;
    if (std.mem.indexOfScalar(u8, path, '/') != null) return isExecutableFile(path);
    const env = path_env orelse return false;
    var buf: [4096]u8 = undefined;
    var it = std.mem.splitScalar(u8, env, ':');
    while (it.next()) |dir| {
        // An empty PATH segment means the current directory (execvp semantics).
        const candidate = if (dir.len == 0)
            std.fmt.bufPrint(&buf, "{s}", .{path}) catch continue
        else
            std.fmt.bufPrint(&buf, "{s}/{s}", .{ dir, path }) catch continue;
        if (isExecutableFile(candidate)) return true;
    }
    return false;
}

/// Raw O_NONBLOCK status bit (packed-struct form is awkward for masking in tests).
const O_NONBLOCK_BIT: u32 = @as(u32, @bitCast(linux.O{ .NONBLOCK = true }));

/// Classifies an inherited std fd by fstat and builds the right sink-mode channel:
///   regular file / block device      -> plain write on the original fd (shared offset + O_APPEND,
///                                        no backpressure; never a private OFD, never flag changes)
/// FIFO / character device -> private NONBLOCK OFD (worker-proof)
///   socket                           -> original fd + send(MSG_DONTWAIT|MSG_NOSIGNAL), flags
///                                        untouched (per-call non-blocking)
///   fstat failure / unknown type     -> conservative plain write on the original fd
fn initOutputChannel(std_fd: i32) OutChan {
    const mode_bits = fstatMode(std_fd) orelse return .{ .fd = std_fd, .mode = .plain };
    if (linux.S.ISREG(mode_bits) or linux.S.ISBLK(mode_bits)) {
        return .{ .fd = std_fd, .mode = .plain };
    }
    if (linux.S.ISFIFO(mode_bits) or linux.S.ISCHR(mode_bits)) {
        if (openPrivateOutputFd(std_fd)) |pfd| {
            return .{ .fd = pfd, .mode = .ofd };
        }
        // An unwritable/unre-openable FIFO or tty sink must never degrade to a blocking
        // .plain write on the original fd — supervision would hang on the stalled sink. Writes
        // are dropped and counted instead (the final drop note reports the count).
        return .{ .fd = std_fd, .mode = .drop };
    }
    if (linux.S.ISSOCK(mode_bits)) {
        return .{ .fd = std_fd, .mode = .sock };
    }
    return .{ .fd = std_fd, .mode = .plain };
}

fn runMain(init: std.process.Init) !u8 {
    installSignalHandlers();
    // Become a child subreaper so Zygote orphans and reparented grandchildren are adopted by us and
    // can always be reaped via waitPidWithDeadline.
    enableChildSubreaper();
    // Output channels by sink type: fstat decides plain (regular file: shared
    // offset, worker writes concatenate), private NONBLOCK OFD (FIFO/tty: worker-proof), or
    // send(MSG_DONTWAIT|MSG_NOSIGNAL) (socket: per-call non-blocking, flags untouched). The flags of
    // inherited descriptors 1/2 are never modified on any path — no save/restore machinery, so no
    // restore-before-flush race and no shared-NONBLOCK the worker could observe or strip.
    g_out_stdout = initOutputChannel(1);
    g_out_stderr = initOutputChannel(2);

    const gpa = init.gpa;
    const io = init.io;
    const args = try init.minimal.args.toSlice(init.arena.allocator());

    var targets: std.ArrayListUnmanaged([]const u8) = .empty;
    defer targets.deinit(gpa);

    const cli = parseCliArgs(gpa, args, &targets) catch |err| {
        if (err == error.UsageError) {
            // Name the offending token when the parser identified one; a usage
            // error that is not about a single argument keeps the generic line.
            if (climod.last_bad_arg) |bad| {
                var ubuf: [1024]u8 = undefined;
                const msg = std.fmt.bufPrint(&ubuf, "ztest: unrecognized argument '{s}' (see --help)\n", .{bad}) catch
                    "ztest: invalid command-line arguments (see --help)\n";
                writeStderr(msg);
            } else {
                writeStderr("ztest: invalid command-line arguments (see --help)\n");
            }
        }
        return err;
    };

    if (cli.show_help) {
        writeStdout(
            \\ztest 0.5.1 (Zig 0.17.0) — hermetic test supervisor for Python (pytest-compatible)
            \\Usage: ztest [options] [file_or_dir] [...]
            \\Options:
            \\  -k <expr>               Filter tests by substring/boolean expression
            \\  -m <expr>               Filter tests by marker expression
            \\  -x, --exitfirst         Stop on first failure
            \\  --maxfail=<N>           Stop after N failures
            \\  --timeout=<sec>         Per-test & supervisor hardware timeout in seconds (0 < sec <= 86400)
            \\  --llm                   JSONL machine-readable output mode
            \\  --engine=native|pytest  Test execution engine (default: native)
            \\  --mode=zygote|exec      Execution mode (default: zygote)
            \\  --preload=<list>        Comma-separated modules to pre-import into the zygote (opt-in)
            \\  --python=<path>         Explicit Python 3.11+ interpreter path
            \\  --collect-only, --co    Only collect and print test nodeids
            \\  -s, --no-capture        Disable stdout/stderr capture
            \\  -v, -q                  Verbose or quiet output
            \\  -j, -n <N>              Parallel workers, 1 to 64 (clamped to 16, default 4)
            \\  -V, --version           Print version
            \\  -h, --help              Show this usage
            \\
        );
        return ExitCode.ok.toU8();
    }
    if (cli.show_version) {
        writeStdout("ztest 0.5.1 (Zig 0.17.0)\n");
        return ExitCode.ok.toU8();
    }

    // Fresh short-summary accumulator per run (the process may re-enter runMain
    // in tests; a stale record would leak into an unrelated summary).
    resetFailedTestNotes();

    var engine_pytest = cli.engine_pytest;
    if (!cli.engine_explicit) {
        if (init.environ_map.get("ZTEST_ENGINE")) |eng_env| {
            if (std.mem.eql(u8, eng_env, "pytest")) {
                engine_pytest = true;
            } else if (std.mem.eql(u8, eng_env, "native") or eng_env.len == 0) {
                engine_pytest = false;
            } else {
                writeStderr("ERROR: ZTEST_ENGINE must be 'native' or 'pytest'\n");
                return error.UsageError;
            }
        }
    }

    // Usage errors are answered here, before any worker exists: an unusable interpreter or a
    // target that is not on disk must fail fast with exit 4. Left to the worker, both cases
    // degenerate into a file-level crash plus Python traceback noise after a supervision window.
    if (cli.explicit_py) |py| {
        if (!interpreterAvailable(py, init.environ_map.get("PATH"))) {
            const msg = try std.fmt.allocPrint(gpa, "ERROR: python interpreter not found or not executable: {s}\n", .{py});
            defer gpa.free(msg);
            writeStderr(msg);
            return error.UsageError;
        }
    }
    if (findMissingTarget(targets.items)) |missing| {
        const msg = try std.fmt.allocPrint(gpa, "ERROR: file or directory not found: {s}\n", .{missing});
        defer gpa.free(msg);
        writeStderr(msg);
        return error.UsageError;
    }

    if (targets.items.len == 0) {
        try targets.append(gpa, "tests");
    }
    // `ztest tests tests` must run each file exactly once — compact duplicate
    // targets before discovery, then dedup the sorted file list so overlapping
    // directory targets also collapse.
    dedupTargets(gpa, &targets);

    const start_ms = monotonicMs();

    var files: std.ArrayListUnmanaged([]u8) = .empty;
    defer {
        for (files.items) |f| gpa.free(f);
        files.deinit(gpa);
    }
    for (targets.items) |t| {
        discoverTestFiles(gpa, t, &files) catch |err| switch (err) {
            error.TooManyPatterns => {
                writeStderr("ztest: error: python_files or norecursedirs exceeds 256 patterns\n");
                return ExitCode.usage_error.toU8();
            },
            else => return err,
        };
    }
    std.mem.sort([]u8, files.items, {}, lessThanStr);
    dedupSortedFileList(gpa, &files);
    // Realpath canonicalization, then a final sort+dedup — string-different
    // spellings of the same file (`./tests/x.py` vs `tests/x.py`, symlinked dirs) collapse to
    // one entry, so each file still runs exactly once even with mixed-style CLI targets.
    try canonicalizeFileList(gpa, &files);
    std.mem.sort([]u8, files.items, {}, lessThanStr);
    dedupSortedFileList(gpa, &files);

    if (files.items.len == 0) {
        if (cli.llm_mode) {
            // Same `collected`/`collection_errors` fields (0 — nothing was
            // ever discovered) for a consistent summary schema.
            writeStdout("{\"t\":\"summary\",\"passed\":0,\"failed\":0,\"skipped\":0,\"deselected\":0,\"crashed\":0,\"collection_errors\":0,\"collected\":0,\"duration_ms\":0}\n");
        } else {
            writeStdout("ztest: no test files discovered.\n");
        }
        return ExitCode.no_tests_collected.toU8();
    }

    // LPT dispatch order for the execute path — largest files
    // (by byte size, the cheapest available size proxy) start first, so a giant file
    // occupies a slot from the start instead of idling the tail (straggler mitigation).
    // files.items keeps the sorted order untouched (collect-only output / summary
    // parity); only dispatch indices are reordered. Disabled for collect_only and
    // maxfail>0: there the visible execution order is semantic and is left untouched.
    // Also disabled at -j1: a single worker processes files
    // sequentially, so reordering gives no win and only diverges from pytest order.
    var dispatch_order: []usize = &.{};
    defer if (dispatch_order.len > 0) gpa.free(dispatch_order);
    if (!cli.collect_only and cli.maxfail == 0 and cli.jobs > 1 and files.items.len > 1) {
        const SizedIdx = struct { size: u64, idx: usize };
        const sized = try gpa.alloc(SizedIdx, files.items.len);
        defer gpa.free(sized);
        var sp_buf: [4096]u8 = undefined;
        for (files.items, 0..) |f, i| {
            const fz = std.fmt.bufPrintSentinel(&sp_buf, "{s}", .{f}, 0) catch {
                sized[i] = .{ .size = 0, .idx = i };
                continue;
            };
            sized[i] = .{ .size = statFileSize(fz), .idx = i };
        }
        std.mem.sort(SizedIdx, sized, {}, struct {
            fn lt(_: void, a: SizedIdx, b: SizedIdx) bool {
                // size desc, idx asc — deterministic tiebreak on the sorted input order.
                if (a.size != b.size) return a.size > b.size;
                return a.idx < b.idx;
            }
        }.lt);
        const order = try gpa.alloc(usize, files.items.len);
        for (sized, 0..) |s, i| order[i] = s.idx;
        dispatch_order = order;
    }

    // Create private 0700 run directory using cryptographic entropy (`getrandom`) and strict `mkdir` check.
    const sup_pid: i32 = @intCast(linux.getpid());
    const sup_uid: u32 = linux.getuid();
    // Fill all 8 entropy bytes from getrandom, retrying short reads/EINTR; abort on real errors
    // so the run-dir suffix is never predictable.
    var rand_bytes: [8]u8 = @splat(0);
    {
        var rand_off: usize = 0;
        var rand_retries: usize = 0;
        while (rand_off < rand_bytes.len) {
            const grc = linux.getrandom(rand_bytes[rand_off..].ptr, rand_bytes.len - rand_off, 0);
            switch (linux.errno(grc)) {
                .SUCCESS => {
                    if (grc == 0) return error.RunDirCreateFailed;
                    rand_off += grc;
                },
                .INTR => {
                    rand_retries += 1;
                    if (rand_retries > 16) return error.RunDirCreateFailed;
                },
                else => return error.RunDirCreateFailed,
            }
        }
    }
    const rand_u64 = std.mem.readInt(u64, &rand_bytes, .little);

    const run_dir = try std.fmt.allocPrint(gpa, "/tmp/ztest_{d}_{d}_{x}", .{ sup_uid, sup_pid, rand_u64 });
    defer gpa.free(run_dir);
    var run_dir_buf: [256]u8 = @splat(0);
    const run_dir_z = try std.fmt.bufPrintSentinel(&run_dir_buf, "{s}", .{run_dir}, 0);
    const mkdir_rc = linux.mkdir(run_dir_z.ptr, 0o700);
    if (linux.errno(mkdir_rc) != .SUCCESS) return error.RunDirCreateFailed;
    // Temp containment: sweep the whole sandbox tree at exit —
    // worker TmpPathFactory bases live under <sandbox>/wtmp, so session temp dirs
    // of SIGKILLed workers are collected here instead of leaking in /tmp. SIGKILL of
    // the supervisor itself still leaves the sandbox behind (documented limit: no
    // parent process sweeps /tmp/ztest_* for us; the leak is bounded to one dir).
    defer std.Io.Dir.cwd().deleteTree(io, run_dir) catch {};
    defer _ = linux.rmdir(run_dir_z.ptr);

    // Verify the directory via O_NOFOLLOW | O_DIRECTORY open
    const dir_probe_rc = linux.open(run_dir_z.ptr, .{ .ACCMODE = .RDONLY, .DIRECTORY = true, .NOFOLLOW = true, .CLOEXEC = true }, 0);
    if (linux.errno(dir_probe_rc) != .SUCCESS) return error.RunDirVerifyFailed;
    _ = linux.close(@intCast(dir_probe_rc));

    const shim_path = try std.fmt.allocPrint(gpa, "{s}/ztest_shim.py", .{run_dir});
    defer gpa.free(shim_path);
    try std.Io.Dir.cwd().writeFile(io, .{ .sub_path = shim_path, .data = EMBEDDED_SHIM });
    defer std.Io.Dir.deleteFileAbsolute(io, shim_path) catch {};

    const ctrl_sock_path = try std.fmt.allocPrint(gpa, "{s}/ctrl.sock", .{run_dir});
    defer gpa.free(ctrl_sock_path);

    const ctrl_listen_fd = try createUnixListenSocket(ctrl_sock_path);
    defer {
        _ = linux.close(ctrl_listen_fd);
        var ubuf: [256]u8 = @splat(0);
        if (std.fmt.bufPrintSentinel(&ubuf, "{s}", .{ctrl_sock_path}, 0)) |uz| {
            _ = linux.unlink(uz.ptr);
        } else |_| {}
    }

    const MAX_SLOTS = 16;
    var slot_sock_paths: [MAX_SLOTS][]u8 = undefined;
    var slot_listen_fds: [MAX_SLOTS]linux.fd_t = @splat(-1);
    var initialized_slots: usize = 0;
    defer {
        for (0..initialized_slots) |si| {
            if (slot_listen_fds[si] >= 0) _ = linux.close(slot_listen_fds[si]);
            var ubuf: [256]u8 = @splat(0);
            if (std.fmt.bufPrintSentinel(&ubuf, "{s}", .{slot_sock_paths[si]}, 0)) |uz| {
                _ = linux.unlink(uz.ptr);
            } else |_| {}
            gpa.free(slot_sock_paths[si]);
        }
    }
    for (0..cli.jobs) |si| {
        slot_sock_paths[si] = try std.fmt.allocPrint(gpa, "{s}/ipc_{d}.sock", .{ run_dir, si });
        slot_listen_fds[si] = try createUnixListenSocket(slot_sock_paths[si]);
        initialized_slots += 1;
    }

    // $VIRTUAL_ENV and $PATH are read once here from the process environment
    // map and passed down, so the resolution logic stays unit-testable without
    // mutating the process env. Reading via `init.environ_map` (not libc
    // getenv) keeps the binary free of libc, so musl targets link statically.
    var venv_buf: [4096]u8 = undefined;
    var path_buf: [4096]u8 = undefined;
    const venv_env: ?[]const u8 = init.environ_map.get("VIRTUAL_ENV");
    const path_env: ?[]const u8 = init.environ_map.get("PATH");
    const py_bin = findPythonBinary(io, cli.explicit_py, venv_env, path_env, &venv_buf, &path_buf);
    const sup_pid_str = try std.fmt.allocPrint(gpa, "{d}", .{sup_pid});
    defer gpa.free(sup_pid_str);

    // Integer microseconds are the only lossless encoding of the accepted
    // (0 < sec <= 86400) f64 range across the JSON boundary. "sandbox" carries the
    // supervisor's run dir so worker TmpPathFactory trees live under it and the
    // supervisor's exit sweep collects them even when a worker was SIGKILLed.
    const opts_json = try std.fmt.allocPrint(
        gpa,
        "{{\"k_expr\":{f},\"m_expr\":{f},\"maxfail\":{d},\"timeout_us\":{d},\"sandbox\":{f},\"no_capture\":{s},\"collect_only\":{s},\"verbose\":{s},\"quiet\":{s},\"llm_mode\":{s},\"engine\":{f}}}",
        .{
            std.json.fmt(cli.k_expr, .{}),
            std.json.fmt(cli.m_expr, .{}),
            cli.maxfail,
            timeoutMicros(cli.timeout_sec),
            std.json.fmt(run_dir, .{}),
            if (cli.no_capture) "true" else "false",
            if (cli.collect_only) "true" else "false",
            if (cli.verbose) "true" else "false",
            if (cli.quiet) "true" else "false",
            if (cli.llm_mode) "true" else "false",
            std.json.fmt(if (engine_pytest) "pytest" else "native", .{}),
        },
    );
    defer gpa.free(opts_json);

    // Per-test inactivity timeout: re-armed on every verified progress frame.
    // The window must dominate the shim's phase itimer, which floors at
    // max(T, 1.5 s) — see workerReadTimeoutMs(). Without --timeout: 1 hour (3_600_000 ms) so long
    // legitimate files are never misreported as crashes (pytest default has no timeout).
    const worker_read_timeout_ms: i64 = workerReadTimeoutMs(cli.timeout_sec);

    // Lazy zygote for single-file suites. One file is a
    // micro-suite: a clean-spawn (exec) is cheaper than the fork protocol and skips the zygote python process.
    // Applied only in the default mode (cli.mode_explicit == false), so an explicit
    // `--mode=zygote` is never overridden; collect_only is untouched (output parity).
    var exec_mode = cli.exec_mode or
        (!cli.mode_explicit and files.items.len == 1 and !cli.collect_only);
    var zygote_conn_fd: ?linux.fd_t = null;
    var zygote_pid: i32 = 0;
    // ADR-004 layer 3: an mmap registry of worker pgids + one native watchdog per run.
    // mmap failure or a failed fork degrades with a warning (containment
    // stays on layers 1+2: the pdeathsig cascade + the zygote EOF watchdog).
    var wd_pid: i32 = 0;
    if (PgidRegistry.create()) |reg| {
        g_wd_reg = reg;
        if (spawnBatchWatchdog(io)) |wp| {
            wd_pid = wp;
            g_wd_pid = wp;
        } else {
            PgidRegistry.destroy(g_wd_reg);
            g_wd_reg = .{ .slots = &.{} };
            writeStderr("ztest: run watchdog spawn failed; degrading to pdeathsig+EOF containment\n");
        }
    } else |_| {
        writeStderr("ztest: watchdog registry mmap failed; degrading to pdeathsig+EOF containment\n");
    }
    // On a normal run end the supervisor itself kills the watchdog (kill by pid) and
    // reaps it — no zombie, no leak. On the supervisor's death the watchdog fires on
    // its own: pidfd-POLLIN → killAll over the registry → _exit. The watchdog's pdeathsig is deliberately
    // not armed: its duty is to kill everything after the supervisor dies.
    defer {
        if (wd_pid > 0) {
            _ = linux.kill(wd_pid, linux.SIG.KILL);
            _ = reapChildBounded(wd_pid, monotonicMs() + 2_000);
            wd_pid = 0;
            g_wd_pid = 0; // reaped: the pid is free for reuse, no sweep may see it again
        }
        if (g_wd_reg.slots.len > 0) {
            PgidRegistry.destroy(g_wd_reg);
            g_wd_reg = .{ .slots = &.{} };
        }
    }
    // Registry of live Zygote-spawned worker PIDs (removed on successful REAP;
    // worker ownership is never lost). Reap statuses are not recorded anywhere.
    // Registered workers stay pinned in protected_pids until their own
    // waitPidWithDeadline finalizes them, so no stale-pid shortcut is needed or safe.
    var zygote_workers: std.ArrayListUnmanaged(i32) = .empty;
    defer zygote_workers.deinit(gpa);
    // Unified ownership registry of all live worker pids (zygote-forked and clean-spawn exec):
    // every sweep that kills/reaps adopted children must treat these as protected so a pinned worker
    // zombie is never released to pid reuse while a slot can still signal it.
    var protected_pids: std.ArrayListUnmanaged(i32) = .empty;
    defer protected_pids.deinit(gpa);

    defer {
        if (zygote_conn_fd) |zfd| {
            writeAllFdDeadline(zfd, "DRAIN\n", monotonicMs() + 2_000) catch {};
            _ = linux.close(zfd);
            zygote_conn_fd = null;
        }
        if (zygote_pid > 0) {
            var z_st: i32 = 0;
            var waited = false;
            for (0..20) |_| {
                const wrc = linux.wait4(zygote_pid, &z_st, linux.W.NOHANG, null);
                if (linux.errno(wrc) != .SUCCESS or wrc > 0) {
                    waited = true;
                    break;
                }
                var req_ts: linux.timespec = .{ .sec = 0, .nsec = 5_000_000 };
                _ = linux.nanosleep(&req_ts, null);
            }
            if (!waited) {
                _ = linux.kill(zygote_pid, linux.SIG.KILL);
                _ = reapChildBounded(zygote_pid, monotonicMs() + 2_000);
            }
            zygote_pid = 0;
        }
        // Final escaped-descendant containment on every exit path (normal, --maxfail, interruption,
        // internal error): anything reparented to us via PR_SET_CHILD_SUBREAPER during teardown —
        // including descendants of interrupted workers — is killed+reaped before we exit, so a test
        // process can never leak past ztest's lifetime (second half).
        // protected is empty here: all slots were already finalized (or killed) by the
        // dispatcher's slot defer, which runs before this one.
        const self_pid: i32 = @intCast(linux.getpid());
        // `null` threshold — every slot is empty here (runMain teardown), so there is no
        // live worker domain left to protect.
        killAdoptedOrphans(self_pid, &.{}, &.{}, null);
        reapAdoptedChildren(&.{});
    }

    if (!exec_mode) {
        var env_map = try init.environ_map.clone(gpa);
        defer env_map.deinit();
        try env_map.put("PYTHONHASHSEED", "0");
        // The preload gate in the shim. A small suite never amortizes the preload
        // cost — there it is pure overhead. The shim's default (variable
        // absent) = preload; a test that really imports a module imports it itself.
        if (files.items.len <= 8) try env_map.put("ZTEST_PRELOAD_HEAVY", "0");
        // Third-party preload is opt-in: `--preload a,b,c` pins the list for the
        // zygote. An exported ZTEST_PRELOAD_PKGS reaches the zygote through the
        // inherited env without the flag; the explicit flag wins.
        if (cli.preload_pkgs.len > 0) try env_map.put("ZTEST_PRELOAD_PKGS", cli.preload_pkgs);
        try env_map.put("ZTEST_ENGINE", if (engine_pytest) "pytest" else "native");

        const z_argv = [_][]const u8{ py_bin, shim_path, "--zygote", ctrl_sock_path, slot_sock_paths[0], sup_pid_str };
        // ADR-004 layer 1: zygote spawn with pdeathsig arming before execve (the
        // supervisor→zygote→workers cascade is executed by the kernel, no ctypes in the shim).
        var z_pid: i32 = 0;
        if (spawnArmed(gpa, &z_argv, &env_map)) |zp| {
            z_pid = zp;
        } else |err| {
            if (err == error.OutOfMemory) return err;
            writeStderr("ztest: zygote spawn failed; falling back to clean-spawn mode\n");
            exec_mode = true;
        }
        if (!exec_mode) zygote_pid = z_pid;
        // The zygote is forked by the supervisor directly (our child): std's Child object
        // is not needed — reap via zygote_pid in the defer/killAndCloseZygote.

        // Accept only makes sense with a live spawn: after a degrade to exec_mode
        // (zygote_pid == 0) the accept would just burn its 15-s deadline for nothing.
        if (!exec_mode) {
            const z_deadline = monotonicMs() + 15_000;
            const z_accept = acceptAuthenticatedWithDeadline(ctrl_listen_fd, sup_uid, zygote_pid, z_deadline, true) catch |err| blk: {
                if (err == error.Interrupted) return error.Interrupted;
                break :blk null;
            };
            if (z_accept) |zfd| {
                zygote_conn_fd = zfd;
                const ready_line = readLineFdDeadline(gpa, zfd, z_deadline) catch |err| blk: {
                    if (err == error.Interrupted) return error.Interrupted;
                    break :blk "";
                };
                defer if (ready_line.len > 0) gpa.free(ready_line);
                if (!std.mem.startsWith(u8, ready_line, "READY\t")) {
                    killAndCloseZygote(&zygote_conn_fd, &zygote_pid, protected_pids.items);
                    exec_mode = true;
                }
            } else {
                killAndCloseZygote(&zygote_conn_fd, &zygote_pid, protected_pids.items);
                exec_mode = true;
            }
        }
    }

    var total_passed: u32 = 0;
    var total_failed: u32 = 0;
    var total_skipped: u32 = 0;
    var total_deselected: u32 = 0;
    var total_crashed: u32 = 0;
    // Session-level collected count (collect-only FILE_DONE frames) —
    // drives the pytest-parity exit 5 when a --collect-only run collects nothing.
    var total_collected: u32 = 0;
    // Files whose collection failed (import/syntax error). pytest reports a collection error
    // as an interrupted session (exit 2), ahead of any plain test failure.
    var total_collection_errors: u32 = 0;

    // ======================================================================================
    // Rolling slot-refill dispatcher.
    // The moment a slot's file is finalized (FILE_DONE + exit + REAP handshake) the slot is
    // refilled with the next dispatch_order file in the same loop iteration, so a giant file
    // never idles the tail of the run and no worker waits for its slowest sibling.
    // --maxfail/-x stops the refill once the failure budget is spent; in-flight slots drain
    // and the run exits when they do.
    // Supervisor pid for the per-slot orphan sweeps and for the quiescent
    // sweep below — hoisted here so both use the same value the final containment uses.
    const self_pid: i32 = @intCast(linux.getpid());

    // Run-scoped slots: `Slot` = ActiveWorker + { empty, active } state. A slot is
    // (re)initialized by a full literal in PHASE B, so no predecessor state can leak
    // (expected_seq, hdr/payload offsets, summary, eof, deadlines, fds).
    var slots: [MAX_SLOTS]RunSlot = @splat(.{});
    var next_file: usize = 0; // queue pointer into dispatch_order
    var running: usize = 0; // slots in .active state

    // Emergency cleanup (SIGINT/SIGTERM, spawn failure, hard IPC error)
    // walks every non-empty slot with the same close/kill/unregister/reap/
    // removePid sequence the normal finalize uses. On a clean run all slots are empty
    // and this is a no-op.
    defer {
        for (&slots) |*slot| {
            if (slot.state != .active) continue;
            abortSlotCleanup(gpa, &slot.aw, &zygote_workers, &protected_pids);
            slot.state = .empty;
        }
    }

    // Parsed JSON never outlives one poll iteration (only the summary
    // fields and the fixed status_buf are copied into aw.summary; payload_buf is gpa),
    // so the arena is reset at the end of every iteration.
    var step_arena = std.heap.ArenaAllocator.init(gpa);
    defer step_arena.deinit();

    while (next_file < files.items.len or running > 0) {
        if (isInterrupted()) return error.Interrupted;

        // PHASE A — finalize every terminal slot. The order is unchanged:
        // close conn/pidfd -> kill-if-bad -> killGroup -> unregister -> REAP/waitpid
        // -> removePid(zygote_workers) -> removePid(protected_pids) -> worker_pid = 0
        // -> aggregate totals -> print per-file line. Exactly-once commit.
        for (&slots) |*slot| {
            if (slot.state != .active) continue;
            const aw = &slot.aw;
            if (!aw.eof) continue;
            // Terminal == stream ended and no post-FILE_DONE exit deadline pending (the
            // pidfd POLLIN or the 3 s grace already discharged it).
            if (aw.worker_pid > 0 and aw.exit_deadline_ms > 0) continue;

            const file_path = aw.file_path;
            const worker_pid = aw.worker_pid;
            const summary = &aw.summary;

            if (aw.conn_fd >= 0) {
                _ = linux.close(aw.conn_fd);
                aw.conn_fd = -1;
            }
            if (aw.worker_pidfd >= 0) {
                _ = linux.close(aw.worker_pidfd);
                aw.worker_pidfd = -1;
            }

            if (summary.timed_out or summary.ipc_error or !summary.got_file_done) {
                // A worker that closed its IPC stream without FILE_DONE (or with an
                // error/timeout) must be dead before REAP so the Zygote's bounded
                // handler never stalls on a hung-after-close worker.
                if (worker_pid > 0) {
                    killGroup(worker_pid);
                    _ = linux.kill(worker_pid, linux.SIG.KILL);
                }
            }

            // ADR-004 layer 3: killGroup + unregister before reap.
            if (worker_pid > 0) killGroup(worker_pid);
            if (worker_pid > 0 and g_wd_reg.slots.len > 0) {
                g_wd_reg.unregister(worker_pid);
            }
            var raw_status: i32 = -1;
            if (!aw.used_exec_mode) {
                if (zygote_conn_fd) |zfd| {
                    const reap_cmd = try std.fmt.allocPrint(gpa, "REAP\t{d}\n", .{worker_pid});
                    defer gpa.free(reap_cmd);
                    const reap_deadline = monotonicMs() + 5_000;
                    const reap_write = writeAllFdDeadline(zfd, reap_cmd, reap_deadline);
                    if (reap_write) |_| {
                        // REAP ownership: from here the numeric pid is not proof of ownership
                        // (see `reap_unconfirmed` and relinquishPidOwnership): every later
                        // numeric signal on this slot is blocked.
                        aw.reap_unconfirmed = true;
                        if (readLineFdDeadline(gpa, zfd, reap_deadline)) |reap_resp| {
                            defer gpa.free(reap_resp);
                            var it = std.mem.splitScalar(u8, reap_resp, '\t');
                            const tag = it.next() orelse "";
                            const pid_s = it.next() orelse "";
                            const st_s = it.next() orelse "";
                            const parsed_pid = std.fmt.parseInt(i32, pid_s, 10) catch -1;
                            if (std.mem.eql(u8, tag, "REAPED") and parsed_pid == worker_pid) {
                                // The matching answer proves our child was reaped —
                                // ownership is resolved and the pid cannot be signaled by
                                // accident any more (it is dead; a recycled pid is a different
                                // process, never our signal target).
                                aw.reap_unconfirmed = false;
                                if (std.fmt.parseInt(i32, st_s, 10)) |st| {
                                    raw_status = st;
                                    removePidFromList(&zygote_workers, worker_pid);
                                } else |_| {
                                    // Malformed REAP status = protocol desync. The
                                    // REAPED answer still proves the reap happened, so only the
                                    // status is lost; bound the (ECHILD) reap without any
                                    // numeric signal.
                                    killAndCloseZygote(&zygote_conn_fd, &zygote_pid, protected_pids.items);
                                    exec_mode = true;
                                    raw_status = reapChildBounded(worker_pid, monotonicMs() + 3_000);
                                    removePidFromList(&zygote_workers, worker_pid);
                                }
                            } else {
                                // Protocol desync: the tag/pid of this answer do not prove that
                                // our pid was reaped, so ownership stays unconfirmed — no
                                // numeric signal (leader/group/reap), signal-free bounded reap
                                // only; the final containment owns whatever is left.
                                killAndCloseZygote(&zygote_conn_fd, &zygote_pid, protected_pids.items);
                                exec_mode = true;
                                raw_status = reapChildBounded(worker_pid, monotonicMs() + 3_000);
                                removePidFromList(&zygote_workers, worker_pid);
                            }
                        } else |rerr| {
                            if (rerr == error.Interrupted) {
                                // the answer is lost while the REAP request is
                                // in flight. Relinquish the numeric pid now and return: the
                                // run-scoped defer (which would otherwise kill leader + group)
                                // honours `reap_unconfirmed`, so nothing signals this pid again.
                                // Containment passes to the group/pidfd paths (pidfd already
                                // closed here) and to the final containment sweep, which only
                                // ever touches our own adopted children.
                                relinquishPidOwnership(aw, "REAP answer interrupted");
                                return error.Interrupted;
                            }
                            // Read timeout / hard read error after a successful REAP write: the
                            // Zygote may already have reaped the child -> ownership stays
                            // unconfirmed -> no numeric signal, signal-free bounded reap only.
                            killAndCloseZygote(&zygote_conn_fd, &zygote_pid, protected_pids.items);
                            exec_mode = true;
                            raw_status = reapChildBounded(worker_pid, monotonicMs() + 3_000);
                            removePidFromList(&zygote_workers, worker_pid);
                        }
                    } else |werr| {
                        // The REAP line is not confirmed delivered, so the pid is still
                        // pinned by the Zygote and remains safe to signal. `writeAllFdDeadline`
                        // either wrote the whole line (then it returns success) or bailed out on
                        // a signal in the middle of the buffer — and a line without its '\n'
                        // is never dispatched by the Zygote's line reader.
                        if (werr == error.Interrupted) return error.Interrupted;
                        killAndCloseZygote(&zygote_conn_fd, &zygote_pid, protected_pids.items);
                        exec_mode = true;
                        raw_status = waitPidWithDeadline(worker_pid, monotonicMs() + 3_000);
                        removePidFromList(&zygote_workers, worker_pid);
                    }
                } else {
                    // Zygote already dead: the worker was adopted by us (subreaper) — reap
                    // it here with its real status (never a fabricated -1).
                    raw_status = waitPidWithDeadline(worker_pid, monotonicMs() + 3_000);
                    removePidFromList(&zygote_workers, worker_pid);
                }
            } else if (worker_pid > 0) {
                // Exec mode: waitPidWithDeadline pins the zombie via WNOWAIT, sweeps
                // -worker_pid, then reaps.
                raw_status = waitPidWithDeadline(worker_pid, monotonicMs() + 3_000);
            }
            // protected_pids is taken off after the reap (the status must be read here).
            // Except when the REAP ownership was lost — then this pid is not ours to
            // release: it stays in the exclude registry, so no sweep of the rest of the run can
            // ever target a recycled pid.
            if (!aw.reap_unconfirmed) removePidFromList(&protected_pids, worker_pid);
            aw.worker_pid = 0;

            const status_slice = summary.status_buf[0..summary.status_len];
            const outcome = if (summary.timed_out or summary.ipc_error)
                WorkerOutcome.crashed
            else
                evaluateWorkerOutcome(summary.got_file_done, summary.failed, status_slice, raw_status);

            // No FILE_DONE ever arrived (crash/kill/IPC loss/timeout): the
            // per-test frames on the wire are the only verified evidence of
            // what finished, so they stand in for the missing counts.
            foldFrameTallies(summary);

            total_passed +|= summary.passed;
            total_failed +|= summary.failed;
            total_skipped +|= summary.skipped;
            total_deselected +|= summary.deselected;
            total_collected +|= summary.collected;
            if (isCollectionErrorStatus(status_slice)) total_collection_errors +|= 1;
            notePartialTally(summary.*, outcome);

            switch (outcome) {
                .passed, .no_tests_selected => {
                    if (!cli.llm_mode and !cli.quiet and !cli.collect_only and outcome == .passed) {
                        const line = try std.fmt.allocPrint(gpa, "{s}✓{s} {s} ({d} passed)\n", .{ cGreen(), cReset(), file_path, summary.passed });
                        defer gpa.free(line);
                        writeStdout(line);
                    }
                },
                .failed => {
                    // Conservative aggregation: failed==0 still counts as a failure.
                    if (summary.failed == 0) total_failed +|= 1;
                    if (!cli.llm_mode and !cli.quiet and !cli.collect_only) {
                        const line = try std.fmt.allocPrint(gpa, "{s}✗{s} {s} ({d} failed, {d} passed)\n", .{ cRed(), cReset(), file_path, summary.failed, summary.passed });
                        defer gpa.free(line);
                        writeStdout(line);
                    }
                },
                .crashed => {
                    total_crashed +|= 1;
                    total_failed +|= 1;
                    const st_u32: u32 = if (raw_status >= 0) @bitCast(raw_status) else 0;
                    const sig: u32 = if (raw_status >= 0 and linux.W.IFSIGNALED(st_u32)) @backingInt(linux.W.TERMSIG(st_u32)) else 0;
                    if (cli.llm_mode) {
                        const line = try std.fmt.allocPrint(gpa, "{{\"t\":\"crash\",\"file\":{f},\"signal\":{d},\"raw_status\":{d},\"timed_out\":{s}}}\n", .{ std.json.fmt(file_path, .{}), sig, raw_status, if (summary.timed_out) "true" else "false" });
                        defer gpa.free(line);
                        writeStdout(line);
                    } else {
                        const line = try std.fmt.allocPrint(gpa, "{s}💀 CRASHED{s} {s} (signal={d}, status={d}, timeout={s})\n", .{ cRed(), cReset(), file_path, sig, raw_status, if (summary.timed_out) "true" else "false" });
                        defer gpa.free(line);
                        writeStdout(line);
                    }
                },
            }

            // Slot is free: the refill phase below can hand it the next file in this
            // iteration — that is the whole point of the rolling dispatcher.
            slot.state = .empty;
            running -= 1;

            // Per-slot orphan sweep right after this slot's finalization: without it a setsid
            // escapee of file #3 could hold a port / write shared tmp / burn CPU until the END
            // of the run. The sweep runs only inside the finalize branch (a poll-only iteration
            // never sweeps), and direct-children enumeration keeps it O(children).
            // protected = live workers of every active slot + the Zygote; a candidate must also
            // be strictly older than the oldest live worker (see killAdoptedOrphans), so a live
            // sibling's helper is spared until a later sweep or the quiescent catch-all.
            {
                var sweep_protected: std.ArrayListUnmanaged(i32) = .empty;
                defer sweep_protected.deinit(gpa);
                try sweep_protected.appendSlice(gpa, protected_pids.items);
                if (zygote_pid > 0) try sweep_protected.append(gpa, zygote_pid);
                for (&slots) |*s| {
                    if (s.state != .active or s.aw.worker_pid <= 0) continue;
                    if (pidInList(sweep_protected.items, s.aw.worker_pid)) continue;
                    try sweep_protected.append(gpa, s.aw.worker_pid);
                }
                reapAdoptedChildren(sweep_protected.items);
                // STARTTIME threshold: sparing `sweep_protected` is safe, but "not protected"
                // is not a licence to kill — a live sibling's helper looks exactly like an
                // escapee of the finished slot. A descendant always starts after its ancestor,
                // so only a candidate strictly older than the oldest live worker is a leftover
                // of a terminated one. `minActiveWorkerStartTicks` reads one /proc/<pid>/stat
                // per live slot and returns `null` when nothing is live, or `0` when some live
                // worker's starttime is unreadable (kill nothing).
                const sweep_older_than = minActiveWorkerStartTicks(&slots);
                killAdoptedOrphans(self_pid, sweep_protected.items, protected_pids.items, sweep_older_than);
            }
        }

        // --maxfail/-x: once the failure budget is spent no new file is dispatched;
        // in-flight slots drain normally and the run exits when the last one finalizes.
        const maxfail_hit = cli.maxfail > 0 and total_failed >= cli.maxfail;

        // PHASE B — refill: hand the next dispatch_order file to every free slot while
        // the run still has files and the jobs budget is not exhausted.
        while (!maxfail_hit and next_file < files.items.len and running < cli.jobs) {
            var slot_idx: usize = MAX_SLOTS;
            for (&slots, 0..) |*s, i| {
                if (s.state == .empty) {
                    slot_idx = i;
                    break;
                }
            }
            if (slot_idx == MAX_SLOTS) break; // unreachable: running < jobs <= MAX_SLOTS
            const dispatch_idx = if (dispatch_order.len > 0) dispatch_order[next_file] else next_file;
            const file_path = files.items[dispatch_idx];
            const slot_sock = slot_sock_paths[slot_idx];
            var worker_pid: i32 = 0;

            if (!exec_mode) {
                const tcount = countProcTasks(zygote_pid) catch 0;
                if (tcount != 1) {
                    writeStderr("ztest: Zygote thread invariant violated, switching to Clean-Spawn\n");
                    killAndCloseZygote(&zygote_conn_fd, &zygote_pid, protected_pids.items);
                    exec_mode = true;
                }
            }

            // OOM window between spawn and publication:
            // `try <list>.append()` after a successful spawn is fallible, and an OOM there
            // would return from runMain with a live, spawned, still-unpublished worker — the
            // slot-defer could not see it (worker_pid is committed to the slot only below) and
            // the Zygote would keep it out of reach of every registry. Reserve the capacity of
            // both pid registries before the fork/spawn, so the post-spawn publication cannot
            // fail and the "every owned worker is registered" invariant holds at the OOM
            // boundary too (the reserve itself is pre-spawn: failing it leaves nothing live).
            try zygote_workers.ensureUnusedCapacity(gpa, 1);
            try protected_pids.ensureUnusedCapacity(gpa, 1);

            var worker_exec_mode = exec_mode;
            if (!worker_exec_mode) {
                const zfd = zygote_conn_fd.?;
                // JSON fork protocol (tab/backslash/newline-safe).
                const cmd = try std.fmt.allocPrint(
                    gpa,
                    "FORK\t{{\"slot\":{f},\"file\":{f},\"opts\":{s}}}\n",
                    .{ std.json.fmt(slot_sock, .{}), std.json.fmt(file_path, .{}), opts_json },
                );
                defer gpa.free(cmd);
                // Deadline-bounded write (2 s): a stalled Zygote cannot block the supervisor
                // beyond this transaction. A connected sibling is protected by the PHASE C
                // ordering instead: all poll-ready descriptors are serviced and their deadlines
                // re-armed before any deadline is applied, so control-plane latency never reads
                // as inactivity.
                const fork_deadline = monotonicMs() + 2_000;
                const fork_write = writeAllFdDeadline(zfd, cmd, fork_deadline);
                if (fork_write) |_| {
                    if (readLineFdDeadline(gpa, zfd, fork_deadline)) |fork_resp| {
                        defer gpa.free(fork_resp);
                        if (std.mem.startsWith(u8, fork_resp, "FORKED\t")) {
                            worker_pid = std.fmt.parseInt(i32, fork_resp["FORKED\t".len..], 10) catch 0;
                            // ADR-004 layer 3: register immediately on FORKED (pgid == pid).
                            if (worker_pid > 0 and g_wd_reg.slots.len > 0) {
                                _ = g_wd_reg.register(worker_pid);
                            }
                        }
                    } else |_| {}
                } else |werr| {
                    if (werr == error.Interrupted) return error.Interrupted;
                }
                // Zygote FORK failure/timeout: kill & close it to prevent protocol desync.
                if (worker_pid <= 0) {
                    killAndCloseZygote(&zygote_conn_fd, &zygote_pid, protected_pids.items);
                    exec_mode = true;
                    worker_exec_mode = true;
                } else {
                    // Registration must never fail silently — abort on OOM.
                    // The capacity of both lists was reserved before the FORK, so this
                    // publication cannot fail and cannot strand a live, unregistered worker.
                    try zygote_workers.append(gpa, worker_pid);
                    try protected_pids.append(gpa, worker_pid);
                }
            }

            if (worker_exec_mode) {
                var env_map = try init.environ_map.clone(gpa);
                defer env_map.deinit();
                try env_map.put("PYTHONHASHSEED", "0");
                try env_map.put("ZTEST_ENGINE", if (engine_pytest) "pytest" else "native");
                const w_argv = [_][]const u8{ py_bin, shim_path, "--worker", slot_sock, file_path, opts_json, sup_pid_str };
                // ADR-004 layer 1: pdeathsig arming before execve.
                if (spawnArmed(gpa, &w_argv, &env_map)) |wp| {
                    worker_pid = wp;
                } else |err| {
                    if (err == error.OutOfMemory) return err;
                    worker_pid = 0;
                    writeStderr("ztest: clean-spawn worker failed to exec\n");
                }
                if (worker_pid > 0) {
                    _ = linux.setpgid(worker_pid, worker_pid);
                    if (g_wd_reg.slots.len > 0) {
                        _ = g_wd_reg.register(worker_pid);
                    }
                    try protected_pids.append(gpa, worker_pid);
                }
            }

            // The full literal resets every predecessor field, and
            // worker_pid is committed before any accept on this slot (so the
            // SO_PEERCRED drain below filters a stale predecessor connection).
            slots[slot_idx].aw = .{
                .file_path = file_path,
                .worker_pid = worker_pid,
                .conn_fd = -1,
                .used_exec_mode = worker_exec_mode,
                .expected_seq = 0,
                .deadline_ms = monotonicMs() + worker_read_timeout_ms,
                // The accept deadline is per spawn.
                .accept_deadline_ms = monotonicMs() + 10_000,
                .eof = false,
                // A refilled slot must never inherit a predecessor's released
                // ownership bit — a fresh spawn owns its numeric pid until its REAP is sent.
                .reap_unconfirmed = false,
                .summary = .{},
            };
            slots[slot_idx].state = .active;
            next_file += 1;
            running += 1;
        }

        if (running == 0) {
            // Refill suppressed by --maxfail with nothing in flight: the run is over.
            if (maxfail_hit) break;
            continue;
        }

        // PHASE C — one multiplexed poll() iteration over the live slots: accepts and
        // connected workers together, ready descriptors serviced
        // first, deadlines applied only afterwards.
        var pfds: [MAX_SLOTS]linux.pollfd = undefined;
        var pfd_to_slot: [MAX_SLOTS]usize = undefined;
        var pfd_is_listen: [MAX_SLOTS]bool = undefined;
        var pfd_is_pidfd: [MAX_SLOTS]bool = undefined;
        var active_pfd_count: usize = 0;
        var earliest_deadline: i64 = std.math.maxInt(i64);
        var pending_exit_slots: usize = 0;

        const now_build = monotonicMs();
        for (&slots, 0..) |*slot, si| {
            if (slot.state != .active) continue;
            const aw = &slot.aw;
            if (aw.eof) {
                if (aw.exit_deadline_ms > 0 and aw.worker_pid > 0) {
                    if (aw.exit_deadline_ms < earliest_deadline) earliest_deadline = aw.exit_deadline_ms;
                    pending_exit_slots += 1;
                    // Instant worker-exit discovery.
                    if (aw.worker_pidfd >= 0) {
                        pfds[active_pfd_count] = .{
                            .fd = aw.worker_pidfd,
                            .events = linux.POLL.IN | linux.POLL.HUP | linux.POLL.ERR,
                            .revents = 0,
                        };
                        pfd_to_slot[active_pfd_count] = si;
                        pfd_is_listen[active_pfd_count] = false;
                        pfd_is_pidfd[active_pfd_count] = true;
                        active_pfd_count += 1;
                    }
                }
                continue;
            }
            if (aw.conn_fd < 0) {
                // Not connected yet: supervise this slot's listen socket until its
                // (per-spawn) accept deadline.
                if (now_build >= aw.accept_deadline_ms) {
                    // Before the deadline turns into a timeout, make
                    // one last non-blocking accept. A worker that connected while the
                    // supervisor was busy elsewhere (REAP handshake / per-slot sweep / FORK of
                    // a sibling) is sitting in this slot's backlog: accepting+servicing it is
                    // correct, killing it unread is a false timeout. `.none` = the backlog is
                    // empty (EAGAIN) and the transition below stands.
                    const late = acceptReadyWorkerAtDeadline(slot_listen_fds[si], sup_uid, worker_read_timeout_ms, aw) catch |err| blk: {
                        if (err == error.Interrupted) return error.Interrupted;
                        break :blk DeadlineAccept.none;
                    };
                    switch (late) {
                        .accepted => {
                            // Installed above with a fresh inactivity deadline. Skip the pfd
                            // arming of this slot: `conn_fd >= 0` now, and arming the stale
                            // listen fd here would poll an already-drained socket against an
                            // EXPIRED accept deadline (wait_ms == 0, busy iteration). The next
                            // PHASE C arms the connected fd and services the frames. Never
                            // billed as a timeout.
                            continue;
                        },
                        .pgid_violation => {
                            // Same kill transition as the STEP-1 accept path.
                            aw.summary.ipc_error = true;
                            aw.summary.got_file_done = false;
                            if (aw.worker_pid > 0) {
                                killGroup(aw.worker_pid);
                                _ = linux.kill(aw.worker_pid, linux.SIG.KILL);
                            }
                            aw.eof = true;
                            continue;
                        },
                        .none => {
                            // An expired accept deadline — the same terminal
                            // transition as in STEP 2.
                            aw.summary.timed_out = true;
                            aw.summary.ipc_error = true;
                            aw.summary.got_file_done = false;
                            finalizeSlotEof(gpa, aw);
                            continue;
                        },
                    }
                }
                if (aw.accept_deadline_ms < earliest_deadline) earliest_deadline = aw.accept_deadline_ms;
                pfds[active_pfd_count] = .{
                    .fd = slot_listen_fds[si],
                    .events = linux.POLL.IN | linux.POLL.HUP | linux.POLL.ERR,
                    .revents = 0,
                };
                pfd_to_slot[active_pfd_count] = si;
                pfd_is_listen[active_pfd_count] = true;
                pfd_is_pidfd[active_pfd_count] = false;
                active_pfd_count += 1;
            } else {
                if (aw.deadline_ms < earliest_deadline) earliest_deadline = aw.deadline_ms;
                pfds[active_pfd_count] = .{
                    .fd = aw.conn_fd,
                    .events = linux.POLL.IN | linux.POLL.HUP | linux.POLL.ERR,
                    .revents = 0,
                };
                pfd_to_slot[active_pfd_count] = si;
                pfd_is_listen[active_pfd_count] = false;
                pfd_is_pidfd[active_pfd_count] = false;
                active_pfd_count += 1;
            }
        }

        // Every live slot is terminal: PHASE A finalizes it on the next iteration (a
        // slot with a pidfd-less exit deadline keeps the loop alive via pending_exit).
        if (active_pfd_count == 0 and pending_exit_slots == 0) continue;

        const wait_ms: i32 = @min(remainingMs(earliest_deadline), 100);
        const prc = linux.poll(&pfds, active_pfd_count, wait_ms);
        switch (linux.errno(prc)) {
            .SUCCESS => {},
            .INTR => {
                if (isInterrupted()) return error.Interrupted;
                continue;
            },
            else => break,
        }

        // STEP 1: service every ready descriptor first.
        if (prc > 0) {
            for (0..active_pfd_count) |pi| {
                if (pfds[pi].revents == 0) continue;
                const si = pfd_to_slot[pi];
                const aw = &slots[si].aw;

                // Worker-exit pidfd: POLLIN == the worker exited.
                if (pfd_is_pidfd[pi]) {
                    if (aw.worker_pidfd >= 0) {
                        _ = linux.close(aw.worker_pidfd);
                        aw.worker_pidfd = -1;
                    }
                    aw.exit_deadline_ms = 0;
                    if (aw.worker_pid > 0) {
                        killGroup(aw.worker_pid);
                    }
                    continue;
                }
                if (aw.eof) continue;

                if (pfd_is_listen[pi]) {
                    if (aw.conn_fd >= 0) continue;
                    // Drain the backlog on this slot's NONBLOCK listen socket: close
                    // stale/foreign peers (a predecessor's leftover connection is
                    // filtered by SO_PEERCRED against the current aw.worker_pid), keep
                    // the verified worker, enforce the pgrp invariant.
                    var slot_done = false;
                    while (!slot_done) {
                        const acc_rc = linux.accept4(slot_listen_fds[si], null, null, linux.SOCK.NONBLOCK | linux.SOCK.CLOEXEC);
                        switch (linux.errno(acc_rc)) {
                            .SUCCESS => {
                                const cfd: linux.fd_t = @intCast(acc_rc);
                                const cred = verifySocketPeerCred(cfd, sup_uid, aw.worker_pid) catch |err| {
                                    _ = linux.close(cfd);
                                    if (err == error.WorkerPidMismatch or err == error.UnauthorizedPeer) continue; // stale backlog
                                    return err;
                                };
                                if (checkPeerPgid(cred.pid) == .violator) {
                                    _ = linux.close(cfd);
                                    aw.summary.ipc_error = true;
                                    aw.summary.got_file_done = false;
                                    if (aw.worker_pid > 0) {
                                        killGroup(aw.worker_pid);
                                        _ = linux.kill(aw.worker_pid, linux.SIG.KILL);
                                    }
                                    aw.eof = true;
                                    slot_done = true;
                                    break;
                                }
                                aw.conn_fd = cfd;
                                aw.worker_pidfd = pidfdOpen(aw.worker_pid);
                                if (g_wd_reg.slots.len > 0) {
                                    _ = g_wd_reg.register(aw.worker_pid);
                                }
                                slot_done = true;
                                // Freshly accepted: spawn latency must not count against
                                // the post-connect inactivity budget.
                                aw.deadline_ms = monotonicMs() + worker_read_timeout_ms;
                                const first_more = drainWorkerFrames(
                                    &step_arena,
                                    gpa,
                                    io,
                                    aw,
                                    worker_read_timeout_ms,
                                    cli.llm_mode,
                                    cli.verbose,
                                    cli.collect_only,
                                ) catch |err| blk: {
                                    if (err == error.Interrupted) return error.Interrupted;
                                    aw.summary.ipc_error = true;
                                    aw.summary.got_file_done = false;
                                    break :blk false;
                                };
                                if (!first_more) finalizeSlotEof(gpa, aw);
                            },
                            .AGAIN => break, // backlog drained; the worker has not connected yet
                            .INTR => {
                                if (isInterrupted()) return error.Interrupted;
                                continue;
                            },
                            else => return error.ReadFailed,
                        }
                    }
                } else {
                    if (aw.conn_fd < 0) continue;
                    const has_more = drainWorkerFrames(
                        &step_arena,
                        gpa,
                        io,
                        aw,
                        worker_read_timeout_ms,
                        cli.llm_mode,
                        cli.verbose,
                        cli.collect_only,
                    ) catch |err| blk: {
                        if (err == error.Interrupted) return error.Interrupted;
                        aw.summary.ipc_error = true;
                        aw.summary.got_file_done = false;
                        break :blk false;
                    };

                    if (!has_more) {
                        finalizeSlotEof(gpa, aw);
                    }
                }
            }
        }

        // STEP 2: only after servicing all ready descriptors, apply inactivity + accept
        // deadlines (a FILE_DONE already sitting in a socket buffer is never billed as
        // a timeout).
        const now_ms = monotonicMs();
        for (&slots, 0..) |*slot, si| {
            if (slot.state != .active) continue;
            const aw = &slot.aw;
            if (aw.eof) {
                if (aw.exit_deadline_ms > 0 and aw.worker_pid > 0 and now_ms >= aw.exit_deadline_ms) {
                    killGroup(aw.worker_pid);
                    _ = linux.kill(aw.worker_pid, linux.SIG.KILL);
                    aw.exit_deadline_ms = 0;
                    if (aw.worker_pidfd >= 0) {
                        _ = linux.close(aw.worker_pidfd);
                        aw.worker_pidfd = -1;
                    }
                }
                continue;
            }
            if (aw.conn_fd >= 0) {
                if (now_ms >= aw.deadline_ms) {
                    aw.summary.timed_out = true;
                    aw.summary.got_file_done = false;
                    finalizeSlotEof(gpa, aw);
                }
            } else if (now_ms >= aw.accept_deadline_ms) {
                // Last-chance non-blocking accept before the accept
                // deadline becomes a timeout — a worker already connected into this slot's
                // backlog (the supervisor was busy with a sibling's REAP/sweep) must be
                // accepted and serviced, not SIGKILLed as "never connected". `.none` (EAGAIN)
                // = nothing was waiting; the timeout transition below then applies.
                const late = acceptReadyWorkerAtDeadline(slot_listen_fds[si], sup_uid, worker_read_timeout_ms, aw) catch |err| blk: {
                    if (err == error.Interrupted) return error.Interrupted;
                    break :blk DeadlineAccept.none;
                };
                switch (late) {
                    .accepted => {}, // serviced by the next PHASE C/STEP 1 (conn_fd set, deadline fresh)
                    .pgid_violation => {
                        aw.summary.ipc_error = true;
                        aw.summary.got_file_done = false;
                        if (aw.worker_pid > 0) {
                            killGroup(aw.worker_pid);
                            _ = linux.kill(aw.worker_pid, linux.SIG.KILL);
                        }
                        aw.eof = true;
                    },
                    .none => {
                        aw.summary.timed_out = true;
                        aw.summary.ipc_error = true;
                        aw.summary.got_file_done = false;
                        finalizeSlotEof(gpa, aw);
                    },
                }
            }
        }

        // Nothing parsed this iteration may be referenced afterwards.
        _ = step_arena.reset(.retain_capacity);
    }

    // Escaped-descendant containment runs after every per-slot
    // finalization inside the loop above, and this quiescent point
    // (next_file == total and running == 0) is the final catch-all for
    // anything that appeared after the last finalization (e.g. a descendant
    // reparented during the teardown of the last slot). It is O(children)
    // via the direct-children path.
    // Emergency exits are covered by the run-scoped final containment defer of runMain.
    {
        var sweep_protected: std.ArrayListUnmanaged(i32) = .empty;
        defer sweep_protected.deinit(gpa);
        try sweep_protected.appendSlice(gpa, protected_pids.items);
        if (zygote_pid > 0) try sweep_protected.append(gpa, zygote_pid);
        reapAdoptedChildren(sweep_protected.items);
        // Quiescent point (next_file == total and running == 0) — no worker of this
        // run is alive, so there is no live sibling domain to protect: `null` threshold selects
        // the kill-everything-not-protected semantics (this is the containment
        // catch-all that removes whatever a starttime-guarded per-slot sweep had to spare
        // earlier in the run, e.g. a live sibling's daemon that outlived its file).
        killAdoptedOrphans(self_pid, sweep_protected.items, protected_pids.items, null);
    }

    const dur_ms: i64 = monotonicMs() - start_ms;

    if (cli.llm_mode) {
        // `collected` joins the llm summary JSON so a
        // --collect-only session shows its real selection size — 0 collected items
        // with exit 5 must not masquerade as an all-zero summary.
        // `collection_errors` is always present (stable machine contract): a
        // bare `failed` count cannot distinguish one failing test from a whole
        // file that died in collection and never ran.
        const line = try std.fmt.allocPrint(
            gpa,
            "{{\"t\":\"summary\",\"passed\":{d},\"failed\":{d},\"skipped\":{d},\"deselected\":{d},\"crashed\":{d},\"collection_errors\":{d},\"collected\":{d},\"duration_ms\":{d}}}\n",
            .{ total_passed, total_failed, total_skipped, total_deselected, total_crashed, total_collection_errors, total_collected, dur_ms },
        );
        defer gpa.free(line);
        writeStdout(line);
    } else if (!cli.collect_only) {
        const color = if (!g_use_color) "" else if (total_failed == 0) "\x1b[32m" else "\x1b[31m";
        // Same distinction for the human summary: with at least one
        // collection error the failed count carries an explicit note; with
        // none the summary line omits the collection-errors segment.
        var ce_buf: [96]u8 = undefined;
        const ce_note: []const u8 = if (total_collection_errors == 0)
            ""
        else if (total_collection_errors == 1)
            " (1 collection error — file not run)"
        else
            std.fmt.bufPrint(&ce_buf, " ({d} collection errors — files not run)", .{total_collection_errors}) catch "";
        // pytest parity: the index of failed tests comes first, then a blank
        // line, then the one-line totals.
        printShortTestSummary();
        const line = try std.fmt.allocPrint(
            gpa,
            "\n{s}ztest summary:{s} {d} passed, {d} failed{s}, {d} skipped, {d} deselected in {d}ms (Zig 0.17.0 {s})\n",
            .{ color, cReset(), total_passed, total_failed, ce_note, total_skipped, total_deselected, dur_ms, if (exec_mode) "Clean-Spawn" else "Zygote-Fork" },
        );
        defer gpa.free(line);
        writeStdout(line);
    }

    // Interruption must never surface as success: exit code 2 even if the last file finished cleanly
    // while SIGTERM/SIGINT was being handled.
    if (isInterrupted()) return error.Interrupted;

    // pytest exit-parity for --collect-only —
    // a session that collected 0 items exits 5 (EXIT_NOTESTSCOLLECTED), not 0.
    // total_collected is fed only by collect-only FILE_DONE frames (`collected`
    // field); the normal executing mode never sends it, so exit 5 keeps
    // covering passed==0 and skipped==0 (all-deselected or NO_TESTS_SELECTED
    // files). A --collect-only run with items keeps exit 0.
    // A failed collection outranks test failures and exits 2, as pytest does.
    return sessionExitCode(false, total_collection_errors, total_failed, total_passed, total_skipped, total_collected);
}

// ============================================================================
// Unit Tests
// ============================================================================

test "20-byte ZTST IPC frame header encode/decode, PID and header+payload CRC32 check" {
    const payload = "{\"t\":\"TEST_PASS\",\"nodeid\":\"test_a.py::test_one\"}";
    const hdr_bytes = encodeFrameHeader(4242, 7, payload);
    try std.testing.expectEqual(@as(usize, 20), hdr_bytes.len);

    const decoded = try decodeAndVerifyFrame(&hdr_bytes, payload, 7, 4242);
    try std.testing.expectEqual(ZTST_MAGIC, decoded.magic);
    try std.testing.expectEqual(@as(i32, 4242), decoded.worker_pid);
    try std.testing.expectEqual(@as(u32, 7), decoded.seq_no);
    try std.testing.expectEqual(@as(u32, @intCast(payload.len)), decoded.payload_len);

    // Wrong expected_pid -> must trigger error.WorkerPidMismatch
    try std.testing.expectError(error.WorkerPidMismatch, decodeAndVerifyFrame(&hdr_bytes, payload, 7, 9999));

    // Corrupt 1 byte in payload -> must trigger error.CrcMismatch
    var bad_payload: [payload.len]u8 = @splat(0);
    @memcpy(&bad_payload, payload);
    bad_payload[0] ^= 0xFF;
    try std.testing.expectError(error.CrcMismatch, decodeAndVerifyFrame(&hdr_bytes, &bad_payload, 7, 4242));

    // Corrupt seq_no in header without updating CRC -> must trigger error.CrcMismatch
    var bad_hdr = hdr_bytes;
    std.mem.writeInt(u32, bad_hdr[8..12], 8, .little);
    try std.testing.expectError(error.CrcMismatch, decodeAndVerifyFrame(&bad_hdr, payload, 8, 4242));
}

test "malformed JSON frames never panic the supervisor" {
    var summary: FileRunSummary = .{};
    try std.testing.expectError(error.MalformedJsonPayload, parseAndApplyFrameJson(std.testing.allocator, null, "[]", &summary, true, false, false));
    try std.testing.expectError(error.MalformedJsonPayload, parseAndApplyFrameJson(std.testing.allocator, null, "{\"t\":42}", &summary, true, false, false));
    try std.testing.expectError(error.MalformedJsonPayload, parseAndApplyFrameJson(std.testing.allocator, null, "{\"t\":\"FILE_DONE\",\"passed\":-1}", &summary, true, false, false));
    try std.testing.expectError(error.MalformedJsonPayload, parseAndApplyFrameJson(std.testing.allocator, null, "{\"t\":\"FILE_DONE\",\"passed\":5.5}", &summary, true, false, false));
}

test "CLI parser rejects inf/trailing/unknown flags and supports -k=, --" {
    const gpa = std.testing.allocator;
    var targets: std.ArrayListUnmanaged([]const u8) = .empty;
    defer targets.deinit(gpa);

    // 1. Reject --timeout=inf
    const bad_inf = [_][]const u8{ "ztest", "--timeout=inf" };
    try std.testing.expectError(error.UsageError, parseCliArgs(gpa, &bad_inf, &targets));

    // 2. Reject trailing -k without value
    const bad_k = [_][]const u8{ "ztest", "-k" };
    try std.testing.expectError(error.UsageError, parseCliArgs(gpa, &bad_k, &targets));

    // 3. Reject unknown flag --bogus
    const bad_unknown = [_][]const u8{ "ztest", "--bogus" };
    try std.testing.expectError(error.UsageError, parseCliArgs(gpa, &bad_unknown, &targets));

    // 4. Accept -k=foo, -m=slow, cosmetic flags (--tb=short, -ra), --, and dash-prefixed target after --
    targets.clearRetainingCapacity();
    const good = [_][]const u8{ "ztest", "-k=foo and not (bar or baz)", "-m=slow", "--tb=short", "-ra", "-j4", "--", "-weird_file.py" };
    const cli = try parseCliArgs(gpa, &good, &targets);
    try std.testing.expectEqualStrings("foo and not (bar or baz)", cli.k_expr);
    try std.testing.expectEqualStrings("slow", cli.m_expr);
    try std.testing.expectEqual(@as(usize, 4), cli.jobs);
    try std.testing.expectEqual(@as(usize, 1), targets.items.len);
    try std.testing.expectEqualStrings("-weird_file.py", targets.items[0]);

    // 5. Reject unimplemented semantic pytest flags (-W, -o, -p, -c, --strict-markers) and malformed -k/-m
    const bad_w = [_][]const u8{ "ztest", "-W", "error" };
    try std.testing.expectError(error.UsageError, parseCliArgs(gpa, &bad_w, &targets));
    const bad_o = [_][]const u8{ "ztest", "-o", "xfail_strict=true" };
    try std.testing.expectError(error.UsageError, parseCliArgs(gpa, &bad_o, &targets));
    const bad_strict = [_][]const u8{ "ztest", "--strict-markers" };
    try std.testing.expectError(error.UsageError, parseCliArgs(gpa, &bad_strict, &targets));
    const bad_k_two = [_][]const u8{ "ztest", "-k", "foo bar" };
    try std.testing.expectError(error.UsageError, parseCliArgs(gpa, &bad_k_two, &targets));
    const bad_k_paren = [_][]const u8{ "ztest", "-k", "(foo" };
    try std.testing.expectError(error.UsageError, parseCliArgs(gpa, &bad_k_paren, &targets));
    const bad_k_trail = [_][]const u8{ "ztest", "-k", "foo and" };
    try std.testing.expectError(error.UsageError, parseCliArgs(gpa, &bad_k_trail, &targets));
}

test "Positive Proof of Pass rejects signals, missing FILE_DONE, negative status, and allows NO_TESTS_SELECTED" {
    try std.testing.expectEqual(WorkerOutcome.passed, evaluateWorkerOutcome(true, 0, "PASSED", 0));
    try std.testing.expectEqual(WorkerOutcome.no_tests_selected, evaluateWorkerOutcome(true, 0, "NO_TESTS_SELECTED", 0));
    try std.testing.expectEqual(WorkerOutcome.crashed, evaluateWorkerOutcome(false, 0, "", 0));
    try std.testing.expectEqual(WorkerOutcome.crashed, evaluateWorkerOutcome(true, 0, "PASSED", 11));
    try std.testing.expectEqual(WorkerOutcome.crashed, evaluateWorkerOutcome(true, 0, "PASSED", -1));
    try std.testing.expectEqual(WorkerOutcome.failed, evaluateWorkerOutcome(true, 2, "FAILED", 256));
}

test "countProcTasks via linux.getdents64(c_uint) and bufPrintSentinel" {
    const self_pid: i32 = @intCast(linux.getpid());
    const tasks = try countProcTasks(self_pid);
    try std.testing.expect(tasks >= 1);
}

test "exit codes round-trip through their u8 wire form; unknown values map to internal_error" {
    try std.testing.expectEqual(@as(u8, 5), ExitCode.no_tests_collected.toU8());
    try std.testing.expectEqual(ExitCode.tests_failed, ExitCode.fromU8(1));
    try std.testing.expectEqual(ExitCode.internal_error, ExitCode.fromU8(99));
}

test "progress frames re-arm the inactivity deadline, terminal/collect frames do not" {
    var summary: FileRunSummary = .{};
    // Progress frames (re-arm the per-test inactivity deadline):
    try std.testing.expectEqual(FrameProgress.progress, try parseAndApplyFrameJson(std.testing.allocator, null, "{\"t\":\"TEST_PASS\",\"nodeid\":\"a::b\"}", &summary, true, false, false));
    try std.testing.expectEqual(FrameProgress.progress, try parseAndApplyFrameJson(std.testing.allocator, null, "{\"t\":\"TEST_SKIP\",\"nodeid\":\"a::b\"}", &summary, true, false, false));
    try std.testing.expectEqual(FrameProgress.progress, try parseAndApplyFrameJson(std.testing.allocator, null, "{\"t\":\"TEST_FAIL\",\"nodeid\":\"a::b\"}", &summary, true, false, false));
    try std.testing.expectEqual(FrameProgress.progress, try parseAndApplyFrameJson(std.testing.allocator, null, "{\"t\":\"COLLECT_BATCH\",\"nodeids\":[\"a::b\"]}", &summary, true, false, false));
    // Heartbeat frames: pure liveness, never mutate the summary, always re-arm:
    try std.testing.expectEqual(FrameProgress.progress, try parseAndApplyFrameJson(std.testing.allocator, null, "{\"t\":\"TEST_START\",\"nodeid\":\"a::b\"}", &summary, true, false, false));
    try std.testing.expectEqual(FrameProgress.progress, try parseAndApplyFrameJson(std.testing.allocator, null, "{\"t\":\"TEST_END\",\"nodeid\":\"a::b\"}", &summary, true, false, false));
    try std.testing.expectEqual(FrameProgress.progress, try parseAndApplyFrameJson(std.testing.allocator, null, "{\"t\":\"TEARDOWN_START\",\"file\":\"a/b.py\"}", &summary, true, false, false));
    // COLLECTION_DONE: closes the collection window before setup_module — re-arms the
    // ordinary per-test budget so a hung setup_module is billed against --timeout.
    try std.testing.expectEqual(FrameProgress.progress, try parseAndApplyFrameJson(std.testing.allocator, null, "{\"t\":\"COLLECTION_DONE\",\"file\":\"a/b.py\"}", &summary, true, false, false));
    try std.testing.expectEqual(@as(u32, 0), summary.passed); // heartbeats never touch counters
    // FILE_START opens the import/collection window: its own classification + bounded budget:
    try std.testing.expectEqual(FrameProgress.file_start, try parseAndApplyFrameJson(std.testing.allocator, null, "{\"t\":\"FILE_START\"}", &summary, true, false, false));
    // Terminal / non-progress frames (no re-arm):
    try std.testing.expectEqual(FrameProgress.none, try parseAndApplyFrameJson(std.testing.allocator, null, "{\"t\":\"COLLECT_ITEM\",\"nodeid\":\"a::b\"}", &summary, true, false, false));
    try std.testing.expectEqual(FrameProgress.none, try parseAndApplyFrameJson(std.testing.allocator, null, "{\"t\":\"FILE_DONE\",\"passed\":1,\"failed\":0,\"skipped\":0,\"deselected\":0,\"status\":\"PASSED\"}", &summary, true, false, false));
    try std.testing.expect(summary.got_file_done);
    try std.testing.expectEqual(@as(u32, 1), summary.passed);
    // Collect-only FILE_DONE frames carry the post-deselection `collected`
    // count (drives the session exit-parity: 0 collected items under --collect-only
    // -> exit 5); the executing-mode FILE_DONE above carries no field -> 0.
    try std.testing.expectEqual(@as(u32, 0), summary.collected);
    try std.testing.expectEqual(FrameProgress.none, try parseAndApplyFrameJson(std.testing.allocator, null, "{\"t\":\"FILE_DONE\",\"passed\":0,\"failed\":0,\"skipped\":0,\"deselected\":2,\"collected\":7,\"status\":\"COLLECTED\"}", &summary, true, false, true));
    try std.testing.expectEqual(@as(u32, 7), summary.collected);
    try std.testing.expectEqual(@as(u32, 2), summary.deselected);
}

test "FILE_START re-arm gets the bounded collection budget" {
    // Small per-test timeout: the import/collection window still gets 10 minutes (bounded floor).
    try std.testing.expectEqual(@as(i64, 600_000), frameBudgetMs(2_500, .file_start));
    try std.testing.expectEqual(@as(i64, 600_000), frameBudgetMs(0, .file_start));
    // Timeout already above the floor: collection budget tracks it (no shrink).
    try std.testing.expectEqual(@as(i64, 3_600_000), frameBudgetMs(3_600_000, .file_start));
    // Ordinary progress and terminal frames: unchanged budget.
    try std.testing.expectEqual(@as(i64, 2_500), frameBudgetMs(2_500, .progress));
    try std.testing.expectEqual(@as(i64, 2_500), frameBudgetMs(2_500, .none));
}

test "getpgid group-leader invariant via linux.syscall1" {
    // Fork a raw (never exec'd) child: it initially shares our process group, so the invariant is false;
    // verifyPeerIsGroupLeader's one setpgid retry is legal pre-exec and must establish it.
    const child = linux.fork();
    if (child == 0) {
        var ts: linux.timespec = .{ .sec = 30, .nsec = 0 };
        _ = linux.nanosleep(&ts, null);
        linux.exit_group(0);
    }
    const child_pid: i32 = @intCast(child);
    defer {
        _ = linux.kill(child_pid, linux.SIG.KILL);
        _ = reapChildBounded(child_pid, monotonicMs() + 2_000);
    }
    const direct_rc = linux.syscall1(.getpgid, @as(usize, @intCast(child_pid)));
    try std.testing.expect(linux.errno(direct_rc) == .SUCCESS);
    try std.testing.expect(direct_rc != @as(usize, @intCast(child_pid))); // not a leader yet
    try std.testing.expect(checkPeerPgid(child_pid) == .leader); // retry fixes it pre-exec
    const after_rc = linux.syscall1(.getpgid, @as(usize, @intCast(child_pid)));
    try std.testing.expect(linux.errno(after_rc) == .SUCCESS);
    try std.testing.expect(after_rc == @as(usize, @intCast(child_pid)));
    // A reaped/dead peer is reported separately (the accept path already proved it).
    _ = linux.kill(child_pid, linux.SIG.KILL);
    _ = reapChildBounded(child_pid, monotonicMs() + 2_000);
    try std.testing.expect(checkPeerPgid(child_pid) == .dead_or_reaped);
    // Invalid PIDs are treated as dead (ESRCH on an absurdly large pid is indistinguishable from dead, which the
    // accept path treats as non-violator only because SO_PEERCRED already proved pid authenticity).
    try std.testing.expect(checkPeerPgid(-1) == .violator);
}

test "writeAllFdDeadline is bounded on a full pipe and succeeds on a writable one" {
    var fds: [2]linux.fd_t = undefined;
    const prc = linux.pipe2(&fds, .{ .CLOEXEC = true, .NONBLOCK = true });
    try std.testing.expect(linux.errno(prc) == .SUCCESS);
    const rfd = fds[0];
    const wfd = fds[1];
    defer _ = linux.close(rfd);
    defer _ = linux.close(wfd);

    // Small write to an empty pipe succeeds immediately.
    try writeAllFdDeadline(wfd, "ZTST", monotonicMs() + 1_000);

    // Fill the 64 KiB pipe buffer completely.
    var filler: [4096]u8 = @splat(0x41);
    var total: usize = 0;
    while (true) {
        const wrc = linux.write(wfd, &filler, filler.len);
        if (linux.errno(wrc) == .SUCCESS) {
            total += wrc;
            if (total > 1 << 20) break; // safety
            continue;
        }
        break; // pipe full (blocking fd: would block -> we stop before; use poll check below)
    }
    try std.testing.expect(total >= 4096);

    // No reader drains the pipe: a further write must time out bounded, not hang.
    const before = monotonicMs();
    try std.testing.expectError(error.WriteTimeout, writeAllFdDeadline(wfd, "X", before + 300));
    const elapsed = monotonicMs() - before;
    try std.testing.expect(elapsed < 2_000);
}

test "reapChildBounded never hangs on a non-child PID" {
    const before = monotonicMs();
    const st = reapChildBounded(0x3FFF_FFF0, before + 5_000);
    try std.testing.expectEqual(@as(i32, -1), st); // ECHILD fast-path
    try std.testing.expect(monotonicMs() - before < 1_000);
}

test "reapAdoptedChildren keeps protected zombies pinned, reaps unregistered without recording status" {
    const child = linux.fork();
    if (child == 0) linux.exit_group(42);
    const child_pid: i32 = @intCast(child);
    // Let it become a zombie.
    var tries: u32 = 0;
    while (tries < 200) : (tries += 1) {
        var sib: [128]u8 align(8) = @splat(0);
        const prc = linux.syscall5(.waitid, 1, @as(usize, @intCast(child_pid)), @intFromPtr(&sib), linux.W.EXITED | linux.W.NOHANG | linux.W.NOWAIT, 0);
        if (linux.errno(prc) == .SUCCESS and std.mem.readInt(i32, sib[16..20], .little) == child_pid) break;
        var ts: linux.timespec = .{ .sec = 0, .nsec = 2_000_000 };
        _ = linux.nanosleep(&ts, null);
    }

    // Protected: sweep must not reap it — the zombie stays pinned.
    const prot = [_]i32{child_pid};
    reapAdoptedChildren(&prot);
    var sib2: [128]u8 align(8) = @splat(0);
    const prc2 = linux.syscall5(.waitid, 1, @as(usize, @intCast(child_pid)), @intFromPtr(&sib2), linux.W.EXITED | linux.W.NOHANG | linux.W.NOWAIT, 0);
    try std.testing.expect(linux.errno(prc2) == .SUCCESS);
    try std.testing.expectEqual(child_pid, std.mem.readInt(i32, sib2[16..20], .little));

    // Unprotected: sweep reaps it for real (without recording the status anywhere).
    reapAdoptedChildren(&.{});
    // The zombie is gone: a targeted WNOWAIT peek now returns ECHILD/no result.
    var sib3: [128]u8 align(8) = @splat(0);
    const prc3 = linux.syscall5(.waitid, 1, @as(usize, @intCast(child_pid)), @intFromPtr(&sib3), linux.W.EXITED | linux.W.NOHANG | linux.W.NOWAIT, 0);
    const errno3 = linux.errno(prc3);
    const reaped = (errno3 != .SUCCESS) or (std.mem.readInt(i32, sib3[16..20], .little) != child_pid);
    try std.testing.expect(reaped);
}

test "finalizeSlotEof kills workers that lost IPC before FILE_DONE" {
    // Case A: stream terminal without FILE_DONE → worker killed immediately.
    const childA = linux.fork();
    if (childA == 0) {
        var ts: linux.timespec = .{ .sec = 30, .nsec = 0 };
        _ = linux.nanosleep(&ts, null);
        linux.exit_group(0);
    }
    const pidA: i32 = @intCast(childA);
    var awA: ActiveWorker = .{ .file_path = "t", .worker_pid = pidA, .conn_fd = -1, .used_exec_mode = true };
    finalizeSlotEof(std.testing.allocator, &awA);
    try std.testing.expect(awA.eof);
    var stA: i32 = -1;
    var waitedA: u32 = 0;
    while (waitedA < 500) : (waitedA += 1) {
        const wrc = linux.wait4(pidA, &stA, linux.W.NOHANG, null);
        if (linux.errno(wrc) == .SUCCESS and wrc != 0) break;
        var ts: linux.timespec = .{ .sec = 0, .nsec = 2_000_000 };
        _ = linux.nanosleep(&ts, null);
    }
    try std.testing.expect(stA != -1); // died (SIGKILL) well under 1s
    try std.testing.expect(linux.W.IFSIGNALED(@bitCast(stA)));

    // Case B: FILE_DONE arrived → clean exiter armed with a 3 s exit deadline, not killed
    // immediately: Phase 4 / the poll loop's exit-deadline step owns the lingering exiter.
    const childB = linux.fork();
    if (childB == 0) {
        var ts: linux.timespec = .{ .sec = 30, .nsec = 0 };
        _ = linux.nanosleep(&ts, null);
        linux.exit_group(0);
    }
    const pidB: i32 = @intCast(childB);
    defer {
        _ = linux.kill(pidB, linux.SIG.KILL);
        _ = reapChildBounded(pidB, monotonicMs() + 2_000);
    }
    var awB: ActiveWorker = .{ .file_path = "t", .worker_pid = pidB, .conn_fd = -1, .used_exec_mode = true };
    awB.summary.got_file_done = true;
    finalizeSlotEof(std.testing.allocator, &awB);
    try std.testing.expect(awB.eof);
    try std.testing.expect(awB.exit_deadline_ms > monotonicMs() + 2_000); // ~3 s budget armed
    try std.testing.expect(linux.errno(linux.kill(pidB, @fromBackingInt(@intCast(0)))) == .SUCCESS); // still alive
    // Firing the one-shot deadline early by hand: kill succeeds and the deadline is cleared once.
    awB.exit_deadline_ms = monotonicMs() - 1;
    killGroup(pidB);
    _ = linux.kill(pidB, linux.SIG.KILL);
    awB.exit_deadline_ms = 0;
    _ = reapChildBounded(pidB, monotonicMs() + 2_000);
}

test "openPrivateOutputFd yields an independent NONBLOCK OFD" {
    const pfd = openPrivateOutputFd(1) orelse return; // environment without /proc: degraded, skip
    defer _ = linux.close(pfd);
    try std.testing.expect(pfd != 1);
    const fl_rc = linux.fcntl(pfd, linux.F.GETFL, 0);
    try std.testing.expect(linux.errno(fl_rc) == .SUCCESS);
    const fl: linux.O = @bitCast(@as(u32, @intCast(fl_rc)));
    try std.testing.expect(fl.NONBLOCK);
}

test "private output OFD preserves caller's O_APPEND" {
    // Unit test of openPrivateOutputFd itself (used for FIFO/tty sinks in production; a regular
    // file is used here only as a hermetic writable sink): re-opening via /proc/self/fd must carry
    // the source's O_APPEND so append semantics are never clobbered by the private copy.
    var path_buf: [64]u8 = undefined;
    const path = std.fmt.bufPrintSentinel(&path_buf, "/tmp/ztest_oappend_test_{d}", .{linux.getpid()}, 0) catch return error.TestUnexpectedResult;
    const src_rc = linux.open(path, .{ .ACCMODE = .WRONLY, .CREAT = true, .TRUNC = true, .APPEND = true, .CLOEXEC = false }, 0o600);
    try std.testing.expect(linux.errno(src_rc) == .SUCCESS);
    const src_fd: linux.fd_t = @intCast(src_rc);
    defer _ = linux.close(src_fd);
    defer _ = linux.unlink(path);
    // Only fd 1/2 semantics matter in production, but openPrivateOutputFd works for any fd via
    // /proc/self/fd/N — exercising it with a private high fd keeps the test hermetic.
    const pfd = openPrivateOutputFd(src_fd) orelse return; // environment without /proc: degraded, skip
    defer _ = linux.close(pfd);
    try std.testing.expect(pfd != src_fd);
    const fl_rc = linux.fcntl(pfd, linux.F.GETFL, 0);
    try std.testing.expect(linux.errno(fl_rc) == .SUCCESS);
    const fl: linux.O = @bitCast(@as(u32, @intCast(fl_rc)));
    try std.testing.expect(fl.NONBLOCK); // supervisor output stays nonblocking
    try std.testing.expect(fl.APPEND); // caller's append semantics carried over (no clobber writes)
    // The private OFD is independent: stripping NONBLOCK on it must not affect the source OFD.
    _ = linux.fcntl(pfd, linux.F.SETFL, @as(usize, @as(u32, @intCast(fl_rc)) & ~O_NONBLOCK_BIT));
    const src_fl_rc = linux.fcntl(src_fd, linux.F.GETFL, 0);
    try std.testing.expect(linux.errno(src_fl_rc) == .SUCCESS);
    const src_fl: linux.O = @bitCast(@as(u32, @intCast(src_fl_rc)));
    try std.testing.expect(src_fl.APPEND); // source untouched
}

test "regular-file sink stays plain, shared offset, interleaved writes intact" {
    // A private OFD on a regular file without O_APPEND would get its own f_pos=0
    // and clobber concurrent writers. The sink-mode redesign keeps regular files on the original
    // fd with the shared file offset, so supervisor and interleaved plain writes concatenate.
    var path_buf: [64]u8 = undefined;
    const path = std.fmt.bufPrintSentinel(&path_buf, "/tmp/ztest_plain_sink_{d}", .{linux.getpid()}, 0) catch return error.TestUnexpectedResult;
    const src_rc = linux.open(path, .{ .ACCMODE = .WRONLY, .CREAT = true, .TRUNC = true }, 0o600);
    try std.testing.expect(linux.errno(src_rc) == .SUCCESS);
    const src_fd: linux.fd_t = @intCast(src_rc);
    defer _ = linux.close(src_fd);
    defer _ = linux.unlink(path);

    var chan = initOutputChannel(src_fd);
    try std.testing.expectEqual(OutSinkMode.plain, chan.mode);
    try std.testing.expectEqual(src_fd, chan.fd); // original fd, no private OFD
    defer chan.backlog.deinit(std.heap.page_allocator);

    // Interleave: a direct write on the same fd, then an OutChan write, then a direct write again.
    _ = linux.write(src_fd, "Sup", "Sup".len);
    outWrite(&chan, "ervi");
    _ = linux.write(src_fd, "sor", "sor".len);
    outFlush(&chan);

    // Read the file back and verify nothing was overwritten (shared offset concatenation).
    const rd_rc = linux.open(path, .{ .ACCMODE = .RDONLY, .CLOEXEC = true }, 0);
    try std.testing.expect(linux.errno(rd_rc) == .SUCCESS);
    const rd_fd: linux.fd_t = @intCast(rd_rc);
    defer _ = linux.close(rd_fd);
    var contents: [32]u8 = @splat(0);
    const rrc = linux.read(rd_fd, &contents, contents.len);
    try std.testing.expect(linux.errno(rrc) == .SUCCESS);
    try std.testing.expectEqualStrings("Supervisor", contents[0..@intCast(rrc)]);
}

test "socket sink uses MSG_DONTWAIT, never blocks, never flips flags" {
    // A fallback that SETFLs the shared socket O_NONBLOCK and restores flags
    // before the final flush lets a blocking write hang the supervisor. The sock sink mode
    // writes with send(MSG_DONTWAIT|MSG_NOSIGNAL): per-call non-blocking, socket flags untouched.
    var fds: [2]linux.fd_t = undefined;
    const sp_rc = linux.socketpair(linux.AF.UNIX, linux.SOCK.STREAM | linux.SOCK.CLOEXEC, 0, &fds);
    try std.testing.expect(linux.errno(sp_rc) == .SUCCESS);
    const a_fd = fds[0];
    const b_fd = fds[1];
    defer _ = linux.close(a_fd);
    defer _ = linux.close(b_fd);

    const fl_before_rc = linux.fcntl(a_fd, linux.F.GETFL, 0);
    try std.testing.expect(linux.errno(fl_before_rc) == .SUCCESS);
    const fl_before: linux.O = @bitCast(@as(u32, @intCast(fl_before_rc)));
    try std.testing.expect(!fl_before.NONBLOCK); // caller's socket is blocking and must stay so

    // Fill the socket buffer with per-call non-blocking sends (flags still untouched).
    var filler: [4096]u8 = @splat(0x41);
    var filled: usize = 0;
    while (true) {
        const rc = linux.sendto(a_fd, &filler, filler.len, MSG_DONTWAIT | MSG_NOSIGNAL, null, 0);
        if (linux.errno(rc) != .SUCCESS) break; // EAGAIN: buffer full
        filled += rc;
        if (filled > (1 << 20)) break; // safety cap
    }
    try std.testing.expect(filled > 0);

    var chan = initOutputChannel(a_fd);
    try std.testing.expectEqual(OutSinkMode.sock, chan.mode);
    try std.testing.expectEqual(a_fd, chan.fd); // original fd: /proc/self/fd cannot reopen sockets
    defer chan.backlog.deinit(std.heap.page_allocator);

    // A write into a full socket must return immediately (MSG_DONTWAIT -> EAGAIN -> backlog).
    const t0 = monotonicMs();
    var payload: [1024]u8 = @splat(0x42);
    outWrite(&chan, &payload);
    const elapsed = monotonicMs() - t0;
    try std.testing.expect(elapsed < 1_000); // did not hang on a full blocking socket
    try std.testing.expectEqual(@as(usize, 1024), chan.backlog.items.len); // buffered whole, not torn
    try std.testing.expectEqual(@as(u64, 0), chan.dropped_bytes);

    // A record that cannot fit in the remaining backlog is dropped WHOLE without modifying backlog.
    const huge = try std.heap.page_allocator.alloc(u8, MAX_OUT_BACKLOG);
    defer std.heap.page_allocator.free(huge);
    @memset(huge, 0x43);
    outWrite(&chan, huge);
    try std.testing.expectEqual(@as(usize, 1024), chan.backlog.items.len); // prior record untouched
    try std.testing.expectEqual(@as(u64, MAX_OUT_BACKLOG), chan.dropped_bytes);

    // An oversized record (> MAX_OUT_BACKLOG) hitting EAGAIN at off == 0 is dropped whole.
    var chan2 = initOutputChannel(a_fd);
    defer chan2.backlog.deinit(std.heap.page_allocator);
    const over = try std.heap.page_allocator.alloc(u8, MAX_OUT_BACKLOG + (512 << 10));
    defer std.heap.page_allocator.free(over);
    @memset(over, 0x44);
    outWrite(&chan2, over);
    try std.testing.expectEqual(@as(usize, 0), chan2.backlog.items.len);
    try std.testing.expectEqual(@as(u64, MAX_OUT_BACKLOG + (512 << 10)), chan2.dropped_bytes);

    // A partial write (off > 0) of a >1 MiB record into a socket with room for only a prefix
    // buffers the entire remaining tail via outBufferTail so the record is never torn in half,
    // while subsequent writes see backlog.len > MAX_OUT_BACKLOG and are dropped whole.
    var drain_buf: [8192]u8 = undefined;
    const rrc = linux.recvfrom(b_fd, &drain_buf, drain_buf.len, MSG_DONTWAIT, null, null);
    try std.testing.expect(linux.errno(rrc) == .SUCCESS and rrc > 0);
    var chan3 = initOutputChannel(a_fd);
    defer chan3.backlog.deinit(std.heap.page_allocator);
    outWrite(&chan3, over);
    try std.testing.expect(chan3.backlog.items.len > 0 and chan3.backlog.items.len < over.len);
    try std.testing.expectEqual(@as(u64, 0), chan3.dropped_bytes);
    outWrite(&chan3, &payload);
    try std.testing.expectEqual(@as(u64, payload.len), chan3.dropped_bytes);

    // Socket flags must be bit-for-bit unchanged (NONBLOCK never flipped by the supervisor).
    const fl_after_rc = linux.fcntl(a_fd, linux.F.GETFL, 0);
    try std.testing.expect(linux.errno(fl_after_rc) == .SUCCESS);
    try std.testing.expectEqual(@as(u32, @intCast(fl_before_rc)), @as(u32, @intCast(fl_after_rc)));
}

test "FIFO sink opens a private NONBLOCK OFD, source fd flags untouched" {
    // FIFO/tty sinks keep the worker-proof private OFD; the original pipe fd must keep its
    // blocking flags so a worker inheriting it observes no flag change from the supervisor.
    var fds: [2]linux.fd_t = undefined;
    const prc = linux.pipe2(&fds, .{ .CLOEXEC = true });
    try std.testing.expect(linux.errno(prc) == .SUCCESS);
    const rfd = fds[0];
    const wfd = fds[1];
    defer _ = linux.close(rfd);
    defer _ = linux.close(wfd);

    const fl_src_before_rc = linux.fcntl(wfd, linux.F.GETFL, 0);
    try std.testing.expect(linux.errno(fl_src_before_rc) == .SUCCESS);
    const fl_src_before: linux.O = @bitCast(@as(u32, @intCast(fl_src_before_rc)));
    try std.testing.expect(!fl_src_before.NONBLOCK); // original pipe write end is blocking

    var chan = initOutputChannel(wfd);
    defer chan.backlog.deinit(std.heap.page_allocator);
    if (chan.mode == .plain) return; // environment without /proc/self/fd re-opening: degraded, skip
    try std.testing.expectEqual(OutSinkMode.ofd, chan.mode);
    try std.testing.expect(chan.fd != wfd); // private OFD, not the inherited one
    const fl_priv_rc = linux.fcntl(chan.fd, linux.F.GETFL, 0);
    try std.testing.expect(linux.errno(fl_priv_rc) == .SUCCESS);
    const fl_priv: linux.O = @bitCast(@as(u32, @intCast(fl_priv_rc)));
    try std.testing.expect(fl_priv.NONBLOCK); // supervisor's copy is nonblocking
    // Source fd flags untouched by the channel setup.
    const fl_src_after_rc = linux.fcntl(wfd, linux.F.GETFL, 0);
    try std.testing.expect(linux.errno(fl_src_after_rc) == .SUCCESS);
    try std.testing.expectEqual(@as(u32, @intCast(fl_src_before_rc)), @as(u32, @intCast(fl_src_after_rc)));
    _ = linux.close(chan.fd);
}

test "unwritable fifo sink degrades to .drop, write never blocks" {
    // A FIFO/tty sink whose private OFD cannot be re-opened (here: write permission
    // stripped via fchmod) must become a .drop channel — writes are counted and discarded.
    // A blocking write on the original fd would hang supervision forever on an
    // unwritable pipe, so the sink never falls back to writing it.
    var fds: [2]linux.fd_t = undefined;
    const prc = linux.pipe2(&fds, .{ .CLOEXEC = true });
    try std.testing.expect(linux.errno(prc) == .SUCCESS);
    const rfd = fds[0];
    const wfd = fds[1];
    defer _ = linux.close(rfd);
    defer _ = linux.close(wfd);

    _ = linux.fchmod(wfd, 0o400); // owner read-only: /proc/self/fd WRONLY reopen now fails EACCES
    var chan = initOutputChannel(wfd);
    defer chan.backlog.deinit(std.heap.page_allocator);
    try std.testing.expectEqual(OutSinkMode.drop, chan.mode);
    try std.testing.expectEqual(wfd, chan.fd); // original fd retained (unused for writes)

    var payload: [1024]u8 = @splat(0x42);
    const t0 = monotonicMs();
    outWrite(&chan, &payload);
    try std.testing.expect(monotonicMs() - t0 < 500); // never blocked, no reader drains rfd
    try std.testing.expectEqual(@as(usize, 0), chan.backlog.items.len); // nothing buffered
    try std.testing.expectEqual(@as(u64, payload.len), chan.dropped_bytes); // counted

    // A flush of a residual backlog in drop mode also only counts (never writes, never blocks).
    chan.backlog.appendSlice(std.heap.page_allocator, "xyz") catch return error.TestUnexpectedResult;
    outFlush(&chan);
    try std.testing.expectEqual(@as(usize, 0), chan.backlog.items.len);
    try std.testing.expectEqual(@as(u64, payload.len + 3), chan.dropped_bytes);

    // A hard write error (EBADF on a closed fd) in outFlushBounded latches both write_err and .drop.
    var bad_chan: OutChan = .{ .fd = -1, .mode = .ofd };
    defer bad_chan.backlog.deinit(std.heap.page_allocator);
    bad_chan.backlog.appendSlice(std.heap.page_allocator, "abc\n") catch return error.TestUnexpectedResult;
    outFlushBounded(&bad_chan, monotonicMs() + 100);
    try std.testing.expect(bad_chan.write_err);
    try std.testing.expectEqual(OutSinkMode.drop, bad_chan.mode);
    try std.testing.expectEqual(@as(u64, 4), bad_chan.dropped_bytes);
}

test "pidfdOpen POLLIN fires on child exit" {
    // The worker-exit discovery primitive: pidfd_open pins the child's identity; poll(POLLIN)
    // reports its exit (zombie included) — event-driven discovery; the 3 s exit deadline bounds
    // only the reap of a lingering clean exiter, never the discovery itself.
    const child = linux.fork();
    if (child == 0) linux.exit_group(0);
    const child_pid: i32 = @intCast(child);
    defer _ = reapChildBounded(child_pid, monotonicMs() + 2_000);

    const pfd = pidfdOpen(child_pid);
    try std.testing.expect(pfd >= 0);
    defer {
        if (pfd >= 0) _ = linux.close(pfd);
    }

    var pollfds = [_]linux.pollfd{.{ .fd = pfd, .events = linux.POLL.IN | linux.POLL.HUP, .revents = 0 }};
    const t0 = monotonicMs();
    var ready = false;
    while (monotonicMs() - t0 < 2_000) {
        const rc = linux.poll(&pollfds, 1, 100);
        if (linux.errno(rc) == .SUCCESS and rc > 0 and (pollfds[0].revents & linux.POLL.IN) != 0) {
            ready = true;
            break;
        }
    }
    try std.testing.expect(ready); // exit signaled through the pidfd, well under the 3 s grace

    // pidfdOpen returns -1 on invalid pids (no crash).
    try std.testing.expectEqual(@as(linux.fd_t, -1), pidfdOpen(-1));
}
test "discovery is symlink-cycle-safe, no exponential duplication" {
    // A directory containing two symlinks that point back at itself
    // (a→., b→.) makes a naive recursion exponential: each level spawns two identical
    // subtrees). The (st_dev, st_ino) visited-set prunes both entries on entry: the symlink
    // path resolves to the already-visited directory inode, so recursion stops immediately
    // and the discovered file list stays unique. Raw syscalls on purpose — mirrors the
    // discovery implementation itself (linux.open/getdents64/symlink).
    const alloc = std.testing.allocator;
    const pid = linux.getpid();
    const root = try std.fmt.allocPrintSentinel(alloc, "/tmp/ztest_cycle_{d}", .{pid}, 0);
    defer alloc.free(root);
    const root_z: [*:0]const u8 = root.ptr;

    const rm_rc0 = linux.rmdir(root_z); // clean any stale leftover from a crashed prior run
    _ = rm_rc0;
    try std.testing.expectEqual(linux.E.SUCCESS, linux.errno(linux.mkdir(root_z, 0o755)));

    const Cleanup = struct {
        fn rmFile(gpa: std.mem.Allocator, dir: []const u8, name: []const u8) void {
            const p = std.fmt.allocPrintSentinel(gpa, "{s}/{s}", .{ dir, name }, 0) catch return;
            defer gpa.free(p);
            _ = linux.unlink(p.ptr);
        }
        fn rmDir(gpa: std.mem.Allocator, dir: []const u8, name: []const u8) void {
            const p = std.fmt.allocPrintSentinel(gpa, "{s}/{s}", .{ dir, name }, 0) catch return;
            defer gpa.free(p);
            _ = linux.rmdir(p.ptr);
        }
    };

    const writePy = struct {
        fn run(gpa: std.mem.Allocator, dir: []const u8, name: []const u8, body: []const u8) !void {
            const p = try std.fmt.allocPrintSentinel(gpa, "{s}/{s}", .{ dir, name }, 0);
            defer gpa.free(p);
            const rc = linux.open(p.ptr, .{ .ACCMODE = .WRONLY, .CREAT = true, .TRUNC = true, .CLOEXEC = true }, 0o644);
            try std.testing.expectEqual(linux.E.SUCCESS, linux.errno(rc));
            const fd: linux.fd_t = @intCast(rc);
            defer _ = linux.close(fd);
            const wrc = linux.write(fd, body.ptr, body.len);
            try std.testing.expectEqual(body.len, wrc);
        }
    }.run;

    try writePy(alloc, root, "test_cycle_a.py", "def test_a():\n    pass\n");
    try writePy(alloc, root, "test_cycle_b.py", "def test_b():\n    pass\n");
    try writePy(alloc, root, "test_cycle_c.py", "def test_c():\n    pass\n");
    // Two self-symlinks (a→., b→.): the exponential cycle.
    {
        const link_a = try std.fmt.allocPrintSentinel(alloc, "{s}/a", .{root}, 0);
        defer alloc.free(link_a);
        try std.testing.expectEqual(linux.E.SUCCESS, linux.errno(linux.symlink(".", link_a.ptr)));
    }
    {
        const link_b = try std.fmt.allocPrintSentinel(alloc, "{s}/b", .{root}, 0);
        defer alloc.free(link_b);
        try std.testing.expectEqual(linux.E.SUCCESS, linux.errno(linux.symlink(".", link_b.ptr)));
    }

    var out: std.ArrayListUnmanaged([]u8) = .empty;
    defer {
        for (out.items) |f| alloc.free(f);
        out.deinit(alloc);
    }
    // discoverTestFiles takes a non-sentinel slice; pass root[0..root.len] — dirKeyOfFd +
    // getdents64 operate on the fd, so discovery itself never needs the sentinel.
    try discoverTestFiles(alloc, root[0..root.len], &out);

    // Exactly the three physical test files, each exactly once (no duplicates from the
    // cycle, no missing file from over-pruning).
    try std.testing.expectEqual(@as(usize, 3), out.items.len);
    var seen: [3]bool = @splat(false);
    for (out.items) |f| {
        if (std.mem.endsWith(u8, f, "/test_cycle_a.py")) seen[0] = true;
        if (std.mem.endsWith(u8, f, "/test_cycle_b.py")) seen[1] = true;
        if (std.mem.endsWith(u8, f, "/test_cycle_c.py")) seen[2] = true;
    }
    try std.testing.expect(seen[0] and seen[1] and seen[2]);

    // Manual cleanup (deinit frees everything): files, symlinks, root dir.
    Cleanup.rmFile(alloc, root, "test_cycle_a.py");
    Cleanup.rmFile(alloc, root, "test_cycle_b.py");
    Cleanup.rmFile(alloc, root, "test_cycle_c.py");
    Cleanup.rmFile(alloc, root, "a");
    Cleanup.rmFile(alloc, root, "b");
    Cleanup.rmDir(alloc, root, "");
}

/// Read the ppid of a live process from /proc/<pid>/stat (same last-')' parsing as
/// killAdoptedOrphans — used by the orphan-sweep test to verify the reparent actually happened.
fn procPpidOf(pid: i32) ?i32 {
    var path_buf: [64]u8 = @splat(0);
    const path = std.fmt.bufPrintSentinel(&path_buf, "/proc/{d}/stat", .{pid}, 0) catch return null;
    const fd_rc = linux.open(path.ptr, .{ .ACCMODE = .RDONLY, .CLOEXEC = true }, 0);
    if (linux.errno(fd_rc) != .SUCCESS) return null;
    const fd: linux.fd_t = @intCast(fd_rc);
    var stat_buf: [512]u8 = @splat(0);
    const rd = linux.read(fd, &stat_buf, stat_buf.len);
    _ = linux.close(fd);
    if (linux.errno(rd) != .SUCCESS or rd <= 0) return null;
    const slice = stat_buf[0..@intCast(rd)];
    const cp = std.mem.lastIndexOfScalar(u8, slice, ')') orelse return null;
    var fields = std.mem.tokenizeScalar(u8, slice[cp + 1 ..], ' ');
    _ = fields.next() orelse return null; // state
    const ppid_s = fields.next() orelse return null;
    return std.fmt.parseInt(i32, ppid_s, 10) catch null;
}

test "killAdoptedOrphans spares protected workers' pgrp (watchdog survival), kills setsid escapees" {
    // Regression guard: a double-forked watchdog of a live worker is adopted by the
    // subreaper (ppid == self) while living in the worker's process group. A sweep without
    // the pgrp filter kills it, silently stripping the worker's supervisor-death protection. The
    // pgrp filter must spare it; a setsid escapee (own pgrp) must still die.
    const self_pid: i32 = @intCast(linux.getpid());
    // PR_SET_CHILD_SUBREAPER: double-forked grandchildren reparent to this process — the exact
    // supervisor topology the orphan sweep runs under.
    _ = linux.syscall2(.prctl, 36, 1);
    defer _ = linux.syscall2(.prctl, 36, 0);

    var pipe_fds: [2]linux.fd_t = undefined;
    const pipe_rc = linux.pipe2(&pipe_fds, .{});
    try std.testing.expect(linux.errno(pipe_rc) == .SUCCESS);
    const rfd = pipe_fds[0];
    const wfd = pipe_fds[1];
    defer _ = linux.close(rfd);

    // W — the "protected worker": own process-group leader, stays alive.
    const w_child = linux.fork();
    if (w_child == 0) {
        _ = linux.setpgid(0, 0);
        // Double-fork a watchdog inside W's process group (no setsid).
        const g1 = linux.fork();
        if (g1 == 0) {
            const g2 = linux.fork();
            if (g2 == 0) {
                var buf: [16]u8 = @splat(0);
                const s = std.fmt.bufPrint(&buf, "W{d}\n", .{linux.getpid()}) catch unreachable;
                _ = linux.write(wfd, s.ptr, s.len);
                var ts: linux.timespec = .{ .sec = 30, .nsec = 0 };
                _ = linux.nanosleep(&ts, null);
                linux.exit_group(0);
            }
            linux.exit_group(0); // intermediate dies → watchdog reparents to the subreaper (self)
        }
        var ts: linux.timespec = .{ .sec = 30, .nsec = 0 };
        _ = linux.nanosleep(&ts, null);
        linux.exit_group(0);
    }
    const w_pid: i32 = @intCast(w_child);
    _ = linux.close(wfd);

    // Read the watchdog's pid announcement.
    var wdog_pid: i32 = -1;
    {
        var rd_buf: [64]u8 = @splat(0);
        var got: usize = 0;
        while (got < rd_buf.len) {
            const rc = linux.read(rfd, rd_buf[got..].ptr, rd_buf.len - got);
            if (linux.errno(rc) != .SUCCESS or rc == 0) break;
            got += @intCast(rc);
            if (std.mem.indexOfScalar(u8, rd_buf[0..got], '\n') != null) break;
        }
        const nl = std.mem.indexOfScalar(u8, rd_buf[0..got], '\n') orelse return error.TestUnexpectedResult;
        wdog_pid = std.fmt.parseInt(i32, rd_buf[1..nl], 10) catch return error.TestUnexpectedResult;
    }
    try std.testing.expect(wdog_pid > 0);

    // Wait until the watchdog really reparented to us (bounded; the kernel does this on the
    // intermediate's death).
    {
        var tries: u32 = 0;
        while (tries < 300) : (tries += 1) {
            if (procPpidOf(wdog_pid) == self_pid) break;
            var ts: linux.timespec = .{ .sec = 0, .nsec = 10_000_000 };
            _ = linux.nanosleep(&ts, null);
        }
        try std.testing.expectEqual(self_pid, procPpidOf(wdog_pid) orelse -1);
    }

    // S — a setsid escapee: double-fork + setsid → own pgrp, reparents to self. Must be killed.
    var pipe2_fds: [2]linux.fd_t = undefined;
    const pipe2_rc = linux.pipe2(&pipe2_fds, .{});
    try std.testing.expect(linux.errno(pipe2_rc) == .SUCCESS);
    const rfd2 = pipe2_fds[0];
    const wfd2 = pipe2_fds[1];
    defer _ = linux.close(rfd2);
    const s_child = linux.fork();
    if (s_child == 0) {
        _ = linux.close(rfd2);
        _ = linux.setsid();
        const s2 = linux.fork();
        if (s2 == 0) {
            var buf: [16]u8 = @splat(0);
            const s = std.fmt.bufPrint(&buf, "{d}\n", .{linux.getpid()}) catch unreachable;
            _ = linux.write(wfd2, s.ptr, s.len);
            _ = linux.close(wfd2);
            var ts: linux.timespec = .{ .sec = 30, .nsec = 0 };
            _ = linux.nanosleep(&ts, null);
            linux.exit_group(0);
        }
        _ = linux.close(wfd2);
        linux.exit_group(0);
    }
    _ = linux.close(wfd2);
    var esc_pid: i32 = -1;
    {
        var rd_buf: [64]u8 = @splat(0);
        var got: usize = 0;
        while (got < rd_buf.len) {
            const rc = linux.read(rfd2, rd_buf[got..].ptr, rd_buf.len - got);
            if (linux.errno(rc) != .SUCCESS or rc == 0) break;
            got += @intCast(rc);
            if (std.mem.indexOfScalar(u8, rd_buf[0..got], '\n') != null) break;
        }
        const nl = std.mem.indexOfScalar(u8, rd_buf[0..got], '\n') orelse return error.TestUnexpectedResult;
        esc_pid = std.fmt.parseInt(i32, rd_buf[0..nl], 10) catch return error.TestUnexpectedResult;
    }
    try std.testing.expect(esc_pid > 0);
    {
        var tries: u32 = 0;
        while (tries < 300) : (tries += 1) {
            if (procPpidOf(esc_pid) == self_pid) break;
            var ts: linux.timespec = .{ .sec = 0, .nsec = 10_000_000 };
            _ = linux.nanosleep(&ts, null);
        }
        try std.testing.expectEqual(self_pid, procPpidOf(esc_pid) orelse -1);
    }

    // The sweep: protected = the live worker W (pid + pgrp). The watchdog (adopted, W's pgrp)
    // must survive; the setsid escapee (adopted, own pgrp) must DIE.
    // `null` threshold on purpose — this test pins the protected-side filters (PID/pgrp);
    // the starttime guard is covered by its own dedicated test.
    killAdoptedOrphans(self_pid, &.{w_pid}, &.{w_pid}, null);

    // Worker alive, watchdog alive.
    try std.testing.expect(linux.errno(linux.kill(w_pid, @fromBackingInt(@intCast(0)))) == .SUCCESS);
    try std.testing.expect(linux.errno(linux.kill(wdog_pid, @fromBackingInt(@intCast(0)))) == .SUCCESS);
    // Escapee dead (kill + reap happened inside the sweep).
    {
        var tries: u32 = 0;
        var dead = false;
        while (tries < 300) : (tries += 1) {
            if (linux.errno(linux.kill(esc_pid, @fromBackingInt(@intCast(0)))) != .SUCCESS) {
                dead = true;
                break;
            }
            var ts: linux.timespec = .{ .sec = 0, .nsec = 10_000_000 };
            _ = linux.nanosleep(&ts, null);
        }
        try std.testing.expect(dead);
    }

    // Cleanup: W and the watchdog share one process group.
    killGroup(w_pid);
    var st_w: i32 = 0;
    var st_g: i32 = 0;
    _ = linux.wait4(w_pid, &st_w, 0, null);
    _ = linux.wait4(wdog_pid, &st_g, 0, null);
}

// ---------------------------------------------------------------------------------------------
// test helpers: a starttime-guarded orphan sweep needs adversarial
// process shapes (live sibling worker / setsid daemon / adopted grandchild) and a second,
// independent way to read `starttime`.
// ---------------------------------------------------------------------------------------------

fn sleepMs(ms: u64) void {
    var ts: linux.timespec = .{ .sec = @intCast(ms / 1000), .nsec = @intCast((ms % 1000) * 1_000_000) };
    _ = linux.nanosleep(&ts, null);
}

fn procAliveG(pid: i32) bool {
    // kill(pid, 0) existence probe without an int->enum detour (same form as the ADR-004 tests).
    return linux.errno(linux.syscall2(.kill, @intCast(pid), 0)) == .SUCCESS;
}

/// Bounded wait until `pid` is gone (SIGKILLed and reaped by the sweep under test).
fn waitUntilDead(pid: i32) bool {
    var tries: u32 = 0;
    while (tries < 300) : (tries += 1) {
        if (!procAliveG(pid)) return true;
        sleepMs(10);
    }
    return false;
}

/// Index cross-check: `starttime` (field 22) the naive way — token index 21 of a plain
/// whitespace split of the whole stat line. Correct only while `comm` carries neither a space nor
/// a ')', which holds for this test binary and for its children; its only purpose is to prove that
/// `parseStartTimeTicks` (last-')' based) reads the same field.
fn naiveStatStartTimeTicks(pid: i32) ?u64 {
    var path_buf: [64]u8 = @splat(0);
    const path = std.fmt.bufPrintSentinel(&path_buf, "/proc/{d}/stat", .{pid}, 0) catch return null;
    const fd_rc = linux.open(path.ptr, .{ .ACCMODE = .RDONLY, .CLOEXEC = true }, 0);
    if (linux.errno(fd_rc) != .SUCCESS) return null;
    const fd: linux.fd_t = @intCast(fd_rc);
    var stat_buf: [512]u8 = @splat(0);
    const rd_rc = linux.read(fd, &stat_buf, stat_buf.len);
    _ = linux.close(fd);
    if (linux.errno(rd_rc) != .SUCCESS or rd_rc <= 0) return null;
    var fields = std.mem.tokenizeScalar(u8, stat_buf[0..@intCast(rd_rc)], ' ');
    var idx: usize = 0;
    while (idx < 21) : (idx += 1) _ = fields.next() orelse return null; // pid .. itrealvalue
    const starttime_s = fields.next() orelse return null;
    return std.fmt.parseInt(u64, starttime_s, 10) catch null;
}

/// A "worker that is still running its file" — own process-group leader (pgid == pid, the
/// invariant the supervisor enforces on every accept), sleeping long enough to outlive the test.
fn forkSleepingPgrpLeader() ?i32 {
    const rc = linux.fork();
    if (linux.errno(rc) != .SUCCESS) return null;
    if (rc == 0) {
        _ = linux.setpgid(0, 0);
        var ts: linux.timespec = .{ .sec = 30, .nsec = 0 };
        _ = linux.nanosleep(&ts, null);
        linux.exit_group(0);
    }
    return @intCast(rc);
}

/// A `setsid` escapee in the exact shape this test is about — the intermediate parent dies at
/// once (so the daemon is adopted by the subreaper, ppid == self) and the daemon leaves the
/// worker's session/process group, escaping the pgrp filter. The daemon announces its pid on a
/// pipe and then sleeps.
fn forkSetsidOrphanAnnounced() ?i32 {
    var fds: [2]linux.fd_t = undefined;
    const prc = linux.pipe2(&fds, .{});
    if (linux.errno(prc) != .SUCCESS) return null;
    const rfd = fds[0];
    const wfd = fds[1];
    const child = linux.fork();
    if (linux.errno(child) != .SUCCESS) {
        _ = linux.close(rfd);
        _ = linux.close(wfd);
        return null;
    }
    if (child == 0) {
        _ = linux.close(rfd);
        _ = linux.setsid();
        const g2 = linux.fork();
        if (g2 == 0) {
            var buf: [16]u8 = @splat(0);
            const s = std.fmt.bufPrint(&buf, "{d}\n", .{linux.getpid()}) catch unreachable;
            _ = linux.write(wfd, s.ptr, s.len);
            _ = linux.close(wfd);
            var ts: linux.timespec = .{ .sec = 30, .nsec = 0 };
            _ = linux.nanosleep(&ts, null);
            linux.exit_group(0);
        }
        _ = linux.close(wfd);
        linux.exit_group(0); // intermediate dies -> daemon reparents to the subreaper (us)
    }
    _ = linux.close(wfd);
    defer _ = linux.close(rfd);
    var rd_buf: [64]u8 = @splat(0);
    var got: usize = 0;
    while (got < rd_buf.len) {
        const rc = linux.read(rfd, rd_buf[got..].ptr, rd_buf.len - got);
        if (linux.errno(rc) != .SUCCESS or rc == 0) break;
        got += @intCast(rc);
        if (std.mem.indexOfScalar(u8, rd_buf[0..got], '\n') != null) break;
    }
    const nl = std.mem.indexOfScalar(u8, rd_buf[0..got], '\n') orelse return null;
    return std.fmt.parseInt(i32, rd_buf[0..nl], 10) catch null;
}

/// Bounded wait until `pid` is a direct child of this process (the subreaper adoption).
fn waitForReparentToSelf(pid: i32) bool {
    const self_pid: i32 = @intCast(linux.getpid());
    var tries: u32 = 0;
    while (tries < 300) : (tries += 1) {
        if (procPpidOf(pid) == self_pid) return true;
        sleepMs(10);
    }
    return false;
}

/// SIGKILL a process-group leader (group + leader) and reap it as our own child.
fn killAndReapChild(pid: i32) void {
    killGroup(pid);
    _ = linux.kill(pid, linux.SIG.KILL);
    var st: i32 = 0;
    _ = linux.wait4(pid, &st, 0, null);
}

test "starttime-guarded per-slot sweep spares a LIVE sibling's setsid daemon, removes provably-finished leftovers per slot, and sweeps everything at quiescence" {
    // The hazard: under the rolling dispatcher the per-slot orphan sweep runs
    // while other files are still executing, and an adopted `setsid` daemon of a live sibling is
    // indistinguishable from an escapee of the finished file by PID/pgrp alone. The guard under
    // test is the kernel's start time: a descendant always starts after its ancestor, so only a
    // candidate strictly older than the oldest live worker is provably a leftover.
    const self_pid: i32 = @intCast(linux.getpid());
    _ = linux.syscall2(.prctl, 36, 1); // PR_SET_CHILD_SUBREAPER — the supervisor topology
    defer _ = linux.syscall2(.prctl, 36, 0);

    // (0) Index sanity: the last-')' parser and the naive 22nd field must agree, otherwise the
    //     whole guard would read (and compare) the wrong column.
    {
        const robust = procStartTimeTicks(self_pid) orelse return error.TestUnexpectedResult;
        const naive = naiveStatStartTimeTicks(self_pid) orelse return error.TestUnexpectedResult;
        try std.testing.expectEqual(naive, robust);
        try std.testing.expect(robust > 0);
    }

    // (1) live sibling W: a worker of a file that is still running.
    const w_pid = forkSleepingPgrpLeader() orelse return error.TestUnexpectedResult;
    var w_reaped = false;
    defer {
        if (!w_reaped) killAndReapChild(w_pid);
    }
    sleepMs(50); // one jiffy+: the daemon below starts strictly later than W

    // (2) SETSID DAEMON S of a terminated intermediate, adopted by us. This single shape covers
    //     both roles at once: to a finished file it is a leftover, to a live sibling it is
    //     a legitimate helper.
    const s_pid = forkSetsidOrphanAnnounced() orelse return error.TestUnexpectedResult;
    try std.testing.expect(s_pid > 0);
    try std.testing.expect(waitForReparentToSelf(s_pid));

    const w_ticks = procStartTimeTicks(w_pid) orelse return error.TestUnexpectedResult;
    const s_ticks = procStartTimeTicks(s_pid) orelse return error.TestUnexpectedResult;
    try std.testing.expect(w_ticks <= s_ticks); // a descendant never predates its ancestor

    // (3) Guard direction "spare": threshold = the live sibling's starttime. S is not strictly
    //     older than W, i.e. it may be W's own descendant -> the sweep must not touch it (nor W).
    killAdoptedOrphans(self_pid, &.{w_pid}, &.{w_pid}, w_ticks);
    try std.testing.expect(procAliveG(s_pid));
    try std.testing.expect(procAliveG(w_pid));

    // (4) Guard direction "kill per slot": W's file finishes (reaped here by hand, which is what
    //     the finalize does), the slot is refilled with a worker that starts later than S. S is now
    // provably older than the oldest live worker -> it cannot belong to worker 2 -> the guarded sweep
    // removes it immediately, while worker 2 keeps running. This is the containment win over
    //     a run-end sweep (which would only act after the last file).
    killAndReapChild(w_pid);
    w_reaped = true;
    sleepMs(50); // next jiffy: starttime(worker 2) > starttime(S)
    const w2_pid = forkSleepingPgrpLeader() orelse return error.TestUnexpectedResult;
    defer killAndReapChild(w2_pid);
    const w2_ticks = procStartTimeTicks(w2_pid) orelse return error.TestUnexpectedResult;
    try std.testing.expect(s_ticks < w2_ticks);
    killAdoptedOrphans(self_pid, &.{w2_pid}, &.{w2_pid}, w2_ticks);
    try std.testing.expect(waitUntilDead(s_pid));
    try std.testing.expect(procAliveG(w2_pid)); // the live sibling's domain is untouched

    // (5) Quiescent direction: with no live worker left the caller passes `null` and the
    //     kill-everything-not-protected semantics apply — a finished worker's daemon dies on this sweep, not at the end of the run.
    const s2_pid = forkSetsidOrphanAnnounced() orelse return error.TestUnexpectedResult;
    try std.testing.expect(waitForReparentToSelf(s2_pid));
    killAdoptedOrphans(self_pid, &.{w2_pid}, &.{w2_pid}, null);
    try std.testing.expect(waitUntilDead(s2_pid));
}

/// test helper: blocking client-side connect to a unix socket path.
fn connectUnixSock(sock_path: []const u8) ?linux.fd_t {
    const sock_rc = linux.socket(linux.AF.UNIX, linux.SOCK.STREAM | linux.SOCK.CLOEXEC, 0);
    if (linux.errno(sock_rc) != .SUCCESS) return null;
    const fd: linux.fd_t = @intCast(sock_rc);
    var addr: linux.sockaddr.un = .{ .family = linux.AF.UNIX, .path = @splat(0) };
    if (sock_path.len >= addr.path.len) {
        _ = linux.close(fd);
        return null;
    }
    @memcpy(addr.path[0..sock_path.len], sock_path);
    const rc = linux.connect(fd, @ptrCast(&addr), @sizeOf(linux.sockaddr.un));
    if (linux.errno(rc) != .SUCCESS) {
        _ = linux.close(fd);
        return null;
    }
    return fd;
}

test "accept-before-timeout: a worker already sitting in the backlog is ACCEPTED (not killed as a timeout), a foreign peer is skipped, an empty backlog still times out" {
    // Regression guard: the accept path must not check an EXPIRED deadline before the last-chance
    // accept, or a worker that connected while the supervisor was busy finalizing a sibling would be
    // SIGKILLed with its backlog unread. The last-chance non-blocking accept is tested here against
    // the three observable outcomes: empty backlog -> .none, foreign backlog entry -> drained + .none,
    // legitimate worker -> .accepted with a re-armed inactivity deadline.
    var path_buf: [128]u8 = @splat(0);
    const path = std.fmt.bufPrint(&path_buf, "/tmp/ztest_accept_{d}.sock", .{linux.getpid()}) catch return error.TestUnexpectedResult;
    const listen_fd = try createUnixListenSocket(path[0..path.len]);
    defer {
        _ = linux.close(listen_fd);
        var z_buf: [160]u8 = @splat(0);
        if (std.fmt.bufPrintSentinel(&z_buf, "{s}", .{path}, 0)) |z| {
            _ = linux.unlink(z.ptr);
        } else |_| {}
    }
    const sup_uid: u32 = linux.getuid();
    const self_pid: i32 = @intCast(linux.getpid());

    var aw: ActiveWorker = .{ .file_path = "file_c.py", .worker_pid = -1, .conn_fd = -1, .used_exec_mode = false };
    defer {
        if (aw.conn_fd >= 0) _ = linux.close(aw.conn_fd);
        if (aw.worker_pidfd >= 0) _ = linux.close(aw.worker_pidfd);
    }

    // (a) EMPTY backlog: EAGAIN -> .none, nothing installed, the timeout transition stands.
    try std.testing.expectEqual(DeadlineAccept.none, try acceptReadyWorkerAtDeadline(listen_fd, sup_uid, 5_000, &aw));
    try std.testing.expect(aw.conn_fd < 0);

    // (b) foreign/stale backlog entry: a peer of our uid whose pid is not this slot's worker (a
    //     predecessor's leftover connection, in the real topology). It must be closed and skipped,
    //     never taken for the worker, and the verdict stays .none.
    const foreign = linux.fork();
    if (linux.errno(foreign) != .SUCCESS) return error.TestUnexpectedResult;
    if (foreign == 0) {
        const cfd = connectUnixSock(path[0..path.len]) orelse linux.exit_group(1);
        var ts: linux.timespec = .{ .sec = 10, .nsec = 0 };
        _ = linux.nanosleep(&ts, null);
        _ = linux.close(cfd);
        linux.exit_group(0);
    }
    const foreign_pid: i32 = @intCast(foreign);
    defer {
        _ = linux.kill(foreign_pid, linux.SIG.KILL);
        var st: i32 = 0;
        _ = linux.wait4(foreign_pid, &st, 0, null);
    }
    // Give the foreign peer the time to enter the backlog, then name a different pid as the slot's
    // worker: the mismatch is what the real accept path filters.
    sleepMs(50);
    aw.worker_pid = self_pid; // != foreign_pid by construction
    try std.testing.expectEqual(DeadlineAccept.none, try acceptReadyWorkerAtDeadline(listen_fd, sup_uid, 5_000, &aw));
    try std.testing.expect(aw.conn_fd < 0);

    // (c) legitimate worker: a child of ours that connects (SO_PEERCRED pid == child pid).
    const child = linux.fork();
    if (linux.errno(child) != .SUCCESS) return error.TestUnexpectedResult;
    if (child == 0) {
        const cfd = connectUnixSock(path[0..path.len]) orelse linux.exit_group(1);
        // Hold the connection open; the supervisor side only needs it to exist in the backlog.
        var ts: linux.timespec = .{ .sec = 10, .nsec = 0 };
        _ = linux.nanosleep(&ts, null);
        _ = linux.close(cfd);
        linux.exit_group(0);
    }
    const worker_pid: i32 = @intCast(child);
    defer {
        _ = linux.kill(worker_pid, linux.SIG.KILL);
        var st: i32 = 0;
        _ = linux.wait4(worker_pid, &st, 0, null);
    }
    aw.worker_pid = worker_pid;
    aw.deadline_ms = 0;
    var verdict: DeadlineAccept = .none;
    var tries: u32 = 0;
    while (tries < 200) : (tries += 1) {
        verdict = try acceptReadyWorkerAtDeadline(listen_fd, sup_uid, 5_000, &aw);
        if (verdict != .none) break;
        sleepMs(10);
    }
    try std.testing.expectEqual(DeadlineAccept.accepted, verdict);
    try std.testing.expect(aw.conn_fd >= 0);
    // The inactivity budget is re-armed, so the accepted-at-deadline connection cannot be billed
    // for the spawn latency (semantics preserved by the last-chance path).
    try std.testing.expect(aw.deadline_ms > monotonicMs());
}

test "relinquishPidOwnership drops every numeric-pid privilege of a slot whose REAP answer was lost" {
    // The hazard: after a `REAP` is written the Zygote may have reaped the child
    // before the answer reaches us; the numeric pid is then free for reuse and must never be
    // signaled again. The primitive that enforces this is unit-tested here: the ownership bit is
    // set and the ADR-004 watchdog registry entry (a numeric `kill(-pgid)` path) is released.
    // The consequence (`reap_unconfirmed` => the run-scoped cleanup defer skips kill/killGroup/
    // reap-by-pid and keeps the pid only as a sweep EXCLUDE) is enforced by the surrounding code.
    const view = try PgidRegistry.create();
    const saved_reg = g_wd_reg;
    g_wd_reg = view;
    defer {
        g_wd_reg = saved_reg;
        PgidRegistry.destroy(view);
    }
    var aw: ActiveWorker = .{ .file_path = "file_d.py", .worker_pid = 4242, .conn_fd = -1, .used_exec_mode = false };
    try std.testing.expect(view.register(4242));
    try std.testing.expect(!aw.reap_unconfirmed);
    relinquishPidOwnership(&aw, "unit test");
    try std.testing.expect(aw.reap_unconfirmed);
    for (view.slots) |*s| {
        try std.testing.expect(@atomicLoad(u64, s, .monotonic) != 4242);
    }
    // Idempotent (the finalize already armed the bit before the failing read).
    relinquishPidOwnership(&aw, "unit test");
    try std.testing.expect(aw.reap_unconfirmed);
}

test "abortSlotCleanup never signals a pid whose REAP ownership was released, and still contains an OWNED one (run early-exit path)" {
    // Fault/state-transition gate on the primitive itself — no real PID reuse needed:
    //   (1) `reap_unconfirmed` (a REAP request went out, the answer was lost) => the still-live
    //       child survives the cleanup untouched: no leader kill, no group kill, no pid-based
    //       reap, no ADR-004 unregister (that layer kills by numeric pgid), and the pid is not
    //       dropped from `protected_pids`, so it stays a sweep EXCLUDE for the rest of the run;
    //   (2) control: the same call on an owned slot (exec-mode = our direct child) kills the
    //       child, reaps it and releases both the registry and the exclude entry.
    const view = try PgidRegistry.create();
    const saved_reg = g_wd_reg;
    g_wd_reg = view;
    defer {
        g_wd_reg = saved_reg;
        PgidRegistry.destroy(view);
    }
    const gpa = std.testing.allocator;
    var zworkers: std.ArrayListUnmanaged(i32) = .empty;
    defer zworkers.deinit(gpa);
    var prot: std.ArrayListUnmanaged(i32) = .empty;
    defer prot.deinit(gpa);

    // ---- (1) Released ownership: a live child must survive the abort cleanup.
    const pid_released = forkSleepingPgrpLeader() orelse return error.TestUnexpectedResult;
    var released_cleaned = false;
    defer {
        // The test itself owns the cleanup of whatever the code under test (correctly) spared.
        if (!released_cleaned) killAndReapChild(pid_released);
    }
    try prot.append(gpa, pid_released);
    try std.testing.expect(view.register(pid_released));
    var aw_released: ActiveWorker = .{
        .file_path = "reap-released",
        .worker_pid = pid_released,
        .conn_fd = -1,
        .used_exec_mode = true,
        .reap_unconfirmed = true,
    };
    abortSlotCleanup(gpa, &aw_released, &zworkers, &prot);
    // worker_pid is zeroed in every case, so no later phase can signal it by number either.
    try std.testing.expectEqual(@as(i32, 0), aw_released.worker_pid);
    sleepMs(50);
    try std.testing.expect(procAliveG(pid_released));
    try std.testing.expect(pidInList(prot.items, pid_released));
    {
        var still_registered = false;
        for (view.slots) |*sl| {
            if (@atomicLoad(u64, sl, .monotonic) == @as(u64, @intCast(pid_released))) still_registered = true;
        }
        try std.testing.expect(still_registered);
    }
    killAndReapChild(pid_released);
    released_cleaned = true;

    // ---- (2) Control: owned slot — the very same call must contain it.
    const pid_owned = forkSleepingPgrpLeader() orelse return error.TestUnexpectedResult;
    var owned_cleaned = false;
    defer {
        if (!owned_cleaned) killAndReapChild(pid_owned);
    }
    try prot.append(gpa, pid_owned);
    try std.testing.expect(view.register(pid_owned));
    var aw_owned: ActiveWorker = .{
        .file_path = "reap-owned",
        .worker_pid = pid_owned,
        .conn_fd = -1,
        .used_exec_mode = true,
        .reap_unconfirmed = false,
    };
    abortSlotCleanup(gpa, &aw_owned, &zworkers, &prot);
    try std.testing.expectEqual(@as(i32, 0), aw_owned.worker_pid);
    try std.testing.expect(waitUntilDead(pid_owned)); // killed by the helper and reaped by it
    try std.testing.expect(!pidInList(prot.items, pid_owned));
    for (view.slots) |*sl| {
        try std.testing.expect(@atomicLoad(u64, sl, .monotonic) != @as(u64, @intCast(pid_owned)));
    }
    var owned_status: i32 = 0;
    // Already reaped by the helper => ECHILD (proof that reaping went to our child, not a recycled pid).
    try std.testing.expectEqual(linux.E.CHILD, linux.errno(linux.wait4(pid_owned, &owned_status, linux.W.NOHANG, null)));
    owned_cleaned = true;
}

test "duplicate targets and sorted duplicate files dedup to one entry each" {
    const alloc = std.testing.allocator;

    // Sorted discovered file list: adjacent duplicates collapse (overlapping targets).
    var files: std.ArrayListUnmanaged([]u8) = .empty;
    defer {
        for (files.items) |f| alloc.free(f);
        files.deinit(alloc);
    }
    for ([_][]const u8{ "a", "a", "b", "b", "b", "c" }) |s| {
        try files.append(alloc, try alloc.dupe(u8, s));
    }
    dedupSortedFileList(alloc, &files);
    try std.testing.expectEqual(@as(usize, 3), files.items.len);
    try std.testing.expectEqualStrings("a", files.items[0]);
    try std.testing.expectEqualStrings("b", files.items[1]);
    try std.testing.expectEqualStrings("c", files.items[2]);

    // Unsorted CLI targets: non-adjacent duplicates keep the first occurrence.
    var targets: std.ArrayListUnmanaged([]const u8) = .empty;
    defer targets.deinit(alloc);
    for ([_][]const u8{ "tests", "other.py", "tests", "tests" }) |s| {
        try targets.append(alloc, s);
    }
    dedupTargets(alloc, &targets);
    try std.testing.expectEqual(@as(usize, 2), targets.items.len);
    try std.testing.expectEqualStrings("tests", targets.items[0]);
    try std.testing.expectEqualStrings("other.py", targets.items[1]);
}

test "realpath canonicalization collapses ./-style duplicates" {
    const alloc = std.testing.allocator;

    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    try tmp.dir.writeFile(std.testing.io, .{ .sub_path = "test_a.py", .data = "" });
    // Relative path of the tmp dir as seen from the test's cwd (tmpDir lives under
    // .zig-cache/tmp/<rand>): canonicalization must resolve it, so test entries use it.
    const rel = try std.fmt.allocPrint(alloc, ".zig-cache/tmp/{s}", .{tmp.sub_path});
    defer alloc.free(rel);

    // Expected canonical path, bootstrapped through the code under test (Zig 0.17 has no
    // public realpath API): canonicalize the plain spelling once, then compare against it.
    var expect_list: std.ArrayListUnmanaged([]u8) = .empty;
    defer {
        for (expect_list.items) |f| alloc.free(f);
        expect_list.deinit(alloc);
    }
    try expect_list.append(alloc, try std.fmt.allocPrint(alloc, "{s}/test_a.py", .{rel}));
    try canonicalizeFileList(alloc, &expect_list);
    const canon = expect_list.items[0];

    var files: std.ArrayListUnmanaged([]u8) = .empty;
    defer {
        for (files.items) |f| alloc.free(f);
        files.deinit(alloc);
    }
    // Two string-different entries for the same file: <rel>/test_a.py and ./<rel>/test_a.py.
    try files.append(alloc, try std.fmt.allocPrint(alloc, "{s}/test_a.py", .{rel}));
    try files.append(alloc, try std.fmt.allocPrint(alloc, "./{s}/test_a.py", .{rel}));
    // A nodeid-style entry with the same file prefix: canonicalize must keep the selector.
    try files.append(alloc, try std.fmt.allocPrint(alloc, "./{s}/test_a.py::test_x", .{rel}));

    try canonicalizeFileList(alloc, &files);
    std.mem.sort([]u8, files.items, {}, lessThanStr);
    dedupSortedFileList(alloc, &files);

    // Plain duplicates collapse to one entry; the ::test_x selector keeps a separate entry.
    try std.testing.expectEqual(@as(usize, 2), files.items.len);
    try std.testing.expectEqualStrings(canon, files.items[0]);
    const with_sel = try std.fmt.allocPrint(alloc, "{s}::test_x", .{canon});
    defer alloc.free(with_sel);
    try std.testing.expectEqualStrings(with_sel, files.items[1]);
}

test "lossless --timeout serialization (integer microseconds)" {
    // 0 / disabled
    try std.testing.expectEqual(@as(i64, 0), timeoutMicros(0.0));
    try std.testing.expectEqual(@as(i64, 0), timeoutMicros(-1.0));
    // sub-10ms values that {d:.2} formatting would truncate to 0.00 must survive intact
    try std.testing.expectEqual(@as(i64, 1000), timeoutMicros(0.001));
    try std.testing.expectEqual(@as(i64, 1500), timeoutMicros(0.0015));
    try std.testing.expectEqual(@as(i64, 150_000), timeoutMicros(0.15));
    try std.testing.expectEqual(@as(i64, 2_000_000), timeoutMicros(2.0));
    try std.testing.expectEqual(@as(i64, 86_400_000_000), timeoutMicros(86400.0));
    // Any accepted positive CLI value stays positive in the worker (never disables the timer)
    var t: f64 = 1e-6;
    while (t < 100.0) : (t *= 10.0) {
        try std.testing.expect(timeoutMicros(t) > 0);
    }
}

test "sub-µs --timeout clamps to 1µs (accepted timeouts never disable the itimer)" {
    // 0 / negative / non-finite: disabled
    try std.testing.expectEqual(@as(i64, 0), timeoutMicros(0.0));
    try std.testing.expectEqual(@as(i64, 0), timeoutMicros(-1.0));
    // sub-µs accepted values: @round gives 0 -> a zero return would mean timer OFF;
    // the clamp keeps every accepted positive value live at >= 1µs
    try std.testing.expectEqual(@as(i64, 1), timeoutMicros(1e-7));
    try std.testing.expectEqual(@as(i64, 1), timeoutMicros(0.0000001));
    try std.testing.expectEqual(@as(i64, 1), timeoutMicros(5e-7));
    // exactly 1µs stays 1µs; 1ms stays 1000µs (semantics preserved)
    try std.testing.expectEqual(@as(i64, 1), timeoutMicros(0.000001));
    try std.testing.expectEqual(@as(i64, 1000), timeoutMicros(0.001));
    try std.testing.expectEqual(@as(i64, 1_500), timeoutMicros(0.0015));
}

test "supervisor window dominates the phase floor: max(T,1.5s)+1.5s" {
    // no --timeout: the generous 1h window (pytest default has no timeout)
    try std.testing.expectEqual(@as(i64, 3_600_000), workerReadTimeoutMs(0.0));
    // T below the 1.5s phase floor: window = 1500 + 1500 (the margin over the
    // 1.5s itimer is the full 1500ms slack, not ~T — a ~T margin is a
    // near-certain kill)
    try std.testing.expectEqual(@as(i64, 3000), workerReadTimeoutMs(0.001));
    try std.testing.expectEqual(@as(i64, 3000), workerReadTimeoutMs(1.0));
    // T above the floor: T*1000 + 1500
    try std.testing.expectEqual(@as(i64, 3500), workerReadTimeoutMs(2.0));
    try std.testing.expectEqual(@as(i64, 11_500), workerReadTimeoutMs(10.0));
    // the 24h+slack ceiling is preserved
    try std.testing.expectEqual(@as(i64, 86_401_500), workerReadTimeoutMs(86400.0));
}

// ============================================================================
// ADR-004 native lifecycle tests (run watchdog + pgid registry + spawnArmed)
// ============================================================================

test "ADR-004: PgidRegistry register/unregister/reuse of slots" {
    const view = try PgidRegistry.create();
    defer PgidRegistry.destroy(view);

    // Register three pgids, read back with atomic loads.
    try std.testing.expect(view.register(100));
    try std.testing.expect(view.register(200));
    try std.testing.expect(view.register(300));
    var seen: usize = 0;
    for (view.slots) |*s| {
        if (@atomicLoad(u64, s, .monotonic) != 0) seen += 1;
    }
    try std.testing.expectEqual(@as(usize, 3), seen);

    // Unregister frees exactly one slot (reuse).
    view.unregister(200);
    seen = 0;
    for (view.slots) |*s| {
        if (@atomicLoad(u64, s, .monotonic) != 0) seen += 1;
    }
    try std.testing.expectEqual(@as(usize, 2), seen);

    // A zero/negative pgid is rejected.
    try std.testing.expect(!view.register(0));
    try std.testing.expect(!view.register(-5));
    // Unregister of an unknown pgid — no-op.
    view.unregister(999);

    // Slot reuse: a new pgid takes the freed slot.
    try std.testing.expect(view.register(400));
    seen = 0;
    for (view.slots) |*s| {
        if (@atomicLoad(u64, s, .monotonic) != 0) seen += 1;
    }
    try std.testing.expectEqual(@as(usize, 3), seen);

    // Full clear.
    view.unregister(100);
    view.unregister(300);
    view.unregister(400);
    for (view.slots) |*s| {
        try std.testing.expectEqual(@as(u64, 0), @atomicLoad(u64, s, .monotonic));
    }
}

test "ADR-004: PgidRegistry overflow is fail-open (documented)" {
    const view = try PgidRegistry.create();
    defer PgidRegistry.destroy(view);

    // Fill all CAPACITY slots. Synthetic ids start at 2: pgid <= 1
    // is rejected by register (pgid=1 under
    // kill(-pgid) = kill(-1) = a broadcast SIGKILL to the whole uid).
    try std.testing.expect(!view.register(0));
    try std.testing.expect(!view.register(1));
    var i: i32 = 2;
    while (i <= PgidRegistry.CAPACITY + 1) : (i += 1) {
        try std.testing.expect(view.register(i));
    }
    // Overflow: register returns false — the registry is fail-open (containment
    // stays on layers 1+2), the run is not torn down. This is documented behavior
    // (see PgidRegistry.View.register): 32 slots >= 2× the max parallelism (16),
    // overflow is reachable only at -j > 16.
    try std.testing.expect(!view.register(99999));

    // killAll on synthetic pgids must not be called: those are live killpg
    // calls (kill(-2) would kill a foreign group). Walking all slots without
    // crashing is verified by reading, without signals. Live groups are covered by the "watchdog integration" test.
    {
        var filled: usize = 0;
        for (view.slots) |*s| {
            if (@atomicLoad(u64, s, .monotonic) != 0) filled += 1;
        }
        try std.testing.expectEqual(PgidRegistry.CAPACITY, filled);
    }

    // Freeing one slot makes the registry available again.
    view.unregister(8);
    try std.testing.expect(view.register(500));
}

test "ADR-004: watchdog integration — mock supervisor death kills sleeping worker groups" {
    // Topology: the test process forks a mock supervisor. The mock creates the
    // pgid registry, forks two sleeping worker process groups (group A = a
    // leader plus an in-group member), registers both pgids, starts the run
    // watchdog (it polls the mock's pidfd) and exits. The watchdog must bring
    // both worker groups down after the mock's death; the parent test
    // process waits and verifies via /proc.

    const mock_rc = linux.fork();
    try std.testing.expect(linux.errno(mock_rc) == .SUCCESS);
    if (mock_rc == 0) {
        // ---- mock supervisor ----
        const view = PgidRegistry.create() catch {
            linux.exit_group(90);
        };
        // Worker group A: a leader process (setpgid) + a group member forked inside
        // the leader (PGID is inherited through fork — the member dies with the same killpg on the group).
        const wa_rc = linux.fork();
        if (wa_rc == 0) {
            _ = linux.setpgid(0, 0);
            const wa2_rc = linux.fork();
            if (wa2_rc == 0) {
                var ts2: linux.timespec = .{ .sec = 30, .nsec = 0 };
                _ = linux.nanosleep(&ts2, null);
                linux.exit_group(0);
            }
            var ts: linux.timespec = .{ .sec = 30, .nsec = 0 };
            _ = linux.nanosleep(&ts, null);
            linux.exit_group(0);
        }
        const wa_pid: i32 = @intCast(wa_rc);
        // Worker group B.
        const wb_rc = linux.fork();
        if (wb_rc == 0) {
            _ = linux.setpgid(0, 0);
            var ts: linux.timespec = .{ .sec = 30, .nsec = 0 };
            _ = linux.nanosleep(&ts, null);
            linux.exit_group(0);
        }
        const wb_pid: i32 = @intCast(wb_rc);
        // Register both groups (as the supervisor's accept-time registration does).
        if (!view.register(wa_pid) or !view.register(wb_pid)) {
            linux.exit_group(91);
        }
        // Watchdog: watches US (the mock).
        g_wd_reg = view;
        _ = spawnBatchWatchdog(std.Io.Threaded.global_single_threaded.io());
        // Let the workers reach nanosleep.
        var ts: linux.timespec = .{ .sec = 0, .nsec = 150_000_000 };
        _ = linux.nanosleep(&ts, null);
        // Mock supervisor death: the watchdog must bring both groups down.
        linux.exit_group(0);
    }
    const mock_pid: i32 = @intCast(mock_rc);
    defer {
        _ = linux.kill(mock_pid, linux.SIG.KILL);
        var st: i32 = 0;
        _ = linux.wait4(mock_pid, &st, 0, null);
    }

    // Wait 3 s: the watchdog should have fired. Collect surviving worker groups via /proc.
    var ts: linux.timespec = .{ .sec = 3, .nsec = 0 };
    _ = linux.nanosleep(&ts, null);

    // The mock did not pass the worker pgids out, so the check is indirect:
    // a worker that survived the watchdog is reparented to init (ppid == 1)
    // and stays live (SIGKILL + reap at init is instant, no zombie). Count
    // live non-zombie /proc entries with ppid == 1 whose comm matches this
    // test binary — the count must be zero.
    const zero = procAliveWithPpid(1);
    try std.testing.expectEqual(@as(usize, 0), zero);
}

test "ADR-004: orphan sweep spares the run watchdog (g_wd_pid exclusion)" {
    // The run watchdog is a direct child
    // of the supervisor with its own pgrp, so killAdoptedOrphans/reapAdoptedChildren
    // must spare it, otherwise layer 3 died from the first sweep on, and the final
    // defer fired SIGKILL at an already-reaped (recycled) pid.
    const saved_wd = g_wd_pid;
    defer g_wd_pid = saved_wd;

    const child_rc = linux.fork();
    try std.testing.expect(linux.errno(child_rc) == .SUCCESS);
    if (child_rc == 0) {
        _ = linux.setpgid(0, 0); // like the watchdog: ppid at the supervisor, own group
        var ts: linux.timespec = .{ .sec = 30, .nsec = 0 };
        _ = linux.nanosleep(&ts, null);
        linux.exit_group(0);
    }
    const child_pid: i32 = @intCast(child_rc);
    g_wd_pid = child_pid;
    defer {
        g_wd_pid = 0;
        _ = linux.kill(child_pid, linux.SIG.KILL);
        var st: i32 = 0;
        _ = linux.wait4(child_pid, &st, 0, null);
    }

    const self_pid: i32 = @intCast(linux.getpid());
    // Empty exclude/protected: the only adopted candidate is our "watchdog".
    killAdoptedOrphans(self_pid, &.{}, &.{}, null);
    reapAdoptedChildren(&.{});
    // The watchdog must survive both sweeps (kill sig=0 — a pure existence check).
    const alive_rc = linux.syscall2(.kill, @intCast(child_pid), 0);
    try std.testing.expect(linux.errno(alive_rc) == .SUCCESS);
}

test "ADR-005 release gate: embedded shim compiles under python3 (anti-drift @embedFile)" {
    // Compilation gate of the EXACT embedded shim — the EMBEDDED_SHIM
    // bytes are written to a temporary file and compiled with python3 through
    // spawnArmed. If python3 is unavailable in the test environment — SkipZigTest.
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    try tmp.dir.writeFile(std.testing.io, .{ .sub_path = "embedded_shim.py", .data = EMBEDDED_SHIM });
    const alloc = std.testing.allocator;
    const script = try std.fmt.allocPrint(alloc, "import sys\ncompile(open('.zig-cache/tmp/{s}/embedded_shim.py','rb').read(), 'embedded_shim', 'exec')\n", .{tmp.sub_path});
    defer alloc.free(script);
    const argv = [_][]const u8{ "python3", "-c", script };
    const pid = spawnArmed(alloc, &argv, null) catch return error.SkipZigTest;
    var st: i32 = 0;
    const wrc = linux.wait4(pid, &st, 0, null);
    try std.testing.expect(linux.errno(wrc) == .SUCCESS);
    const stu: u32 = @bitCast(st);
    try std.testing.expect(linux.W.IFEXITED(stu));
    try std.testing.expectEqual(@as(u32, 0), linux.W.EXITSTATUS(stu));
}

fn procAliveWithPpid(target_ppid: i32) usize {
    // Count live (non-zombie) processes with the given PPID whose comm matches
    // this test binary's comm. Zombies (state 'Z') do not count as live.
    var alive: usize = 0;

    // Read our binary's comm once before walking /proc.
    var self_comm_buf: [64]u8 = @splat(0);
    var self_comm: []const u8 = "";
    {
        const self_name = "/proc/self/comm";
        var self_z: [16]u8 = @splat(0);
        @memcpy(self_z[0..self_name.len], self_name);
        const sf_rc = linux.open(@ptrCast(&self_z), .{ .ACCMODE = .RDONLY, .CLOEXEC = true }, 0);
        if (linux.errno(sf_rc) == .SUCCESS) {
            const sf: linux.fd_t = @intCast(sf_rc);
            defer _ = linux.close(sf);
            const r = linux.read(sf, &self_comm_buf, self_comm_buf.len - 1);
            if (linux.errno(r) == .SUCCESS) {
                self_comm = std.mem.trimEnd(u8, self_comm_buf[0..r], "\n\x00");
            }
        }
    }

    const open_rc = linux.open("/proc", .{ .ACCMODE = .RDONLY, .DIRECTORY = true, .CLOEXEC = true }, 0);
    if (linux.errno(open_rc) != .SUCCESS) return 0;
    const dir_fd: linux.fd_t = @intCast(open_rc);
    defer _ = linux.close(dir_fd);
    var buf: [8192]u8 align(@alignOf(LinuxDirent64)) = @splat(0);
    while (true) {
        const n = linux.getdents64(dir_fd, &buf, @intCast(buf.len));
        if (linux.errno(n) != .SUCCESS or n == 0) break;
        var pos: usize = 0;
        while (pos < n) {
            const d: *const LinuxDirent64 = @ptrCast(@alignCast(buf[pos..].ptr));
            if (d.d_reclen == 0) break;
            pos += d.d_reclen;
            // d_name — a flexible tail: read the name through a sentinel-ptr cast
            // (the canonical form of the other getdents64 loops in this file), not via [0.. :0] on
            // the [1]u8 array — else the sentinel safety check catches the second byte
            // of the name (".." → panic 'sentinel mismatch: expected 0, found 46').
            const name_ptr: [*:0]const u8 = @ptrCast(&d.d_name[0]);
            const name = std.mem.span(name_ptr);
            if (name.len == 0 or name[0] < '0' or name[0] > '9') continue;
            _ = std.fmt.parseInt(i32, name, 10) catch continue;
            // /proc/<pid>/stat: after ')' come state, ppid. Block scope: the fd
            // is closed by the defer on every iteration, not on function exit.
            blk: {
                var path_buf: [64]u8 = undefined;
                const path = std.fmt.bufPrint(&path_buf, "/proc/{s}/stat", .{name}) catch break :blk;
                var path_z_buf: [64]u8 = undefined;
                @memcpy(path_z_buf[0..path.len], path);
                path_z_buf[path.len] = 0;
                const stat_open_rc = linux.open(@ptrCast(&path_z_buf), .{ .ACCMODE = .RDONLY, .CLOEXEC = true }, 0);
                if (linux.errno(stat_open_rc) != .SUCCESS) break :blk;
                const fd: linux.fd_t = @intCast(stat_open_rc);
                defer _ = linux.close(fd);
                var stat_buf: [512]u8 = @splat(0);
                const sr = linux.read(fd, &stat_buf, stat_buf.len - 1);
                if (linux.errno(sr) != .SUCCESS or sr == 0) break :blk;
                const stat = stat_buf[0..sr];
                const close_paren = std.mem.lastIndexOfScalar(u8, stat, ')') orelse break :blk;
                var it = std.mem.tokenizeScalar(u8, stat[close_paren + 1 ..], ' ');
                const state = it.next() orelse break :blk;
                const ppid_s = it.next() orelse break :blk;
                const ppid = std.fmt.parseInt(i32, ppid_s, 10) catch break :blk;
                if (ppid != target_ppid) break :blk;
                if (state.len > 0 and state[0] == 'Z') break :blk; // a zombie is not live
                // Only children of our test binary: the mock workers' comm
                // matches the test process's comm (inherited through fork).
                const open_paren = std.mem.indexOfScalar(u8, stat, '(') orelse break :blk;
                const comm = stat[open_paren + 1 .. close_paren];
                if (self_comm.len > 0 and
                    std.mem.startsWith(u8, comm, self_comm[0..@min(self_comm.len, comm.len)]))
                {
                    alive += 1;
                }
            }
        }
    }
    return alive;
}

test "python_files from ini/pyproject drives discovery (custom tests.py pattern)" {
    // glob matcher
    try std.testing.expect(globMatch("test_*.py", "test_x.py"));
    try std.testing.expect(globMatch("*_test.py", "x_test.py"));
    // norecursedirs — basename glob and path suffix
    {
        var nrp: PyFilePatterns = .{};
        nrp.pats[0] = ".tox";
        nrp.pats[1] = "tests/mypy_test_cases";
        nrp.len = 2;
        try std.testing.expect(norecurseMatch(&nrp, ".tox", "x/.tox"));
        try std.testing.expect(norecurseMatch(&nrp, "mypy_test_cases", "tests/mypy_test_cases"));
        try std.testing.expect(norecurseMatch(&nrp, "mypy_test_cases", "a/tests/mypy_test_cases"));
        try std.testing.expect(!norecurseMatch(&nrp, "tests", "tests"));
        try std.testing.expect(!norecurseMatch(&nrp, "mypy", "tests/mypy"));
        // ini scanner
        var nrp2: PyFilePatterns = .{};
        const ini = "[pytest]\nnorecursedirs = .git venv tests/skip_me\naddopts = -q\n";
        try std.testing.expect(scanIniNorecursedirs(ini, "pytest", &nrp2) == .found_key);
        try std.testing.expect(nrp2.len == 3);
        try std.testing.expect(norecurseMatch(&nrp2, "skip_me", "tests/skip_me"));
        // toml scanner (quoted value line)
        var nrp3: PyFilePatterns = .{};
        const toml = "[tool.pytest.ini_options]\nnorecursedirs = \".tox tests/mypy_test_cases\"\n";
        try std.testing.expect(scanTomlNorecursedirs(toml, &nrp3) == .found_key);
        try std.testing.expect(nrp3.len == 2);
        try std.testing.expect(norecurseMatch(&nrp3, "mypy_test_cases", "tests/mypy_test_cases"));
    }
    try std.testing.expect(!globMatch("tests.py", "test_x.py"));
    try std.testing.expect(globMatch("tests.py", "tests.py"));
    try std.testing.expect(!globMatch("test_*.py", "xtest_a.py"));
    // defaults
    var defs = defaultPyFilePatterns();
    try std.testing.expect(isTestFileNamePats("test_a.py", &defs));
    try std.testing.expect(isTestFileNamePats("a_test.py", &defs));
    try std.testing.expect(!isTestFileNamePats("tests.py", &defs));
    // TOML scan: table form
    var t1: PyFilePatterns = .{};
    try std.testing.expectEqual(PyFileScan.found_key, scanTomlPythonFiles(
        "[project]\nname='x'\n[tool.pytest.ini_options]\npython_files = \"tests.py\"\n",
        &t1,
    ));
    try std.testing.expectEqual(@as(usize, 1), t1.len);
    try std.testing.expect(isTestFileNamePats("tests.py", &t1));
    try std.testing.expect(!isTestFileNamePats("test_a.py", &t1));
    // TOML scan: dotted-key form (dotted-key table)
    var t2: PyFilePatterns = .{};
    try std.testing.expectEqual(PyFileScan.found_key, scanTomlPythonFiles(
        "[tool.pytest]\nini_options.python_files = [ \"tests.py\" ]\n",
        &t2,
    ));
    try std.testing.expectEqual(@as(usize, 1), t2.len);
    // INI scan: two patterns separated by a space
    var t3: PyFilePatterns = .{};
    try std.testing.expectEqual(PyFileScan.found_key, scanIniPythonFiles(
        "[pytest]\npython_files = tests.py check_*.py\n",
        "pytest",
        &t3,
    ));
    try std.testing.expectEqual(@as(usize, 2), t3.len);
    try std.testing.expect(isTestFileNamePats("check_a.py", &t3));
    // Multiline TOML array + >8 patterns + '?' and '[...]' globs + multiline INI
    try std.testing.expect(globMatch("test_?.py", "test_1.py"));
    try std.testing.expect(!globMatch("test_?.py", "test_12.py"));
    try std.testing.expect(globMatch("test_[ab].py", "test_b.py"));
    try std.testing.expect(globMatch("test_[!c].py", "test_a.py"));
    try std.testing.expect(!globMatch("test_[!c].py", "test_c.py"));
    try std.testing.expect(globMatch("test_[0-9].py", "test_7.py"));
    var t_ml: PyFilePatterns = .{};
    try std.testing.expectEqual(PyFileScan.found_key, scanTomlPythonFiles(
        "[tool.pytest.ini_options]\npython_files = [\n  \"p1_*.py\",\n  \"p2_*.py\", # comment\n  \"p3_*.py\",\n  \"p4_*.py\",\n  \"p5_*.py\",\n  \"p6_*.py\",\n  \"p7_*.py\",\n  \"p8_*.py\",\n  \"p9_?.py\",\n  \"p10_[ab].py\",\n  \"[ab]p11_*.py\",\n]\n",
        &t_ml,
    ));
    try std.testing.expectEqual(@as(usize, 11), t_ml.len);
    try std.testing.expect(isTestFileNamePats("p9_x.py", &t_ml));
    try std.testing.expect(isTestFileNamePats("p10_b.py", &t_ml));
    try std.testing.expect(isTestFileNamePats("ap11_foo.py", &t_ml));
    var t_ini_ml: PyFilePatterns = .{};
    try std.testing.expectEqual(PyFileScan.found_key, scanIniPythonFiles(
        "[pytest]\npython_files =\n    tests.py\n    check_*.py\naddopts = -q\n",
        "pytest",
        &t_ini_ml,
    ));
    try std.testing.expectEqual(@as(usize, 2), t_ini_ml.len);
    try std.testing.expect(isTestFileNamePats("check_foo.py", &t_ini_ml));
    // no section / no key
    var t4: PyFilePatterns = .{};
    try std.testing.expectEqual(PyFileScan.no_section, scanTomlPythonFiles("[project]\nname='x'\n", &t4));
    var t5: PyFilePatterns = .{};
    try std.testing.expectEqual(PyFileScan.no_key, scanIniPythonFiles("[pytest]\naddopts = -q\n", "pytest", &t5));

    // end-to-end discovery: pyproject python_files="tests.py" => only tests.py
    const alloc = std.testing.allocator;
    const pid = linux.getpid();
    const root = try std.fmt.allocPrintSentinel(alloc, "/tmp/ztest_pyfiles_{d}", .{pid}, 0);
    defer alloc.free(root);
    _ = linux.rmdir(root.ptr);
    try std.testing.expectEqual(linux.E.SUCCESS, linux.errno(linux.mkdir(root.ptr, 0o755)));
    const W = struct {
        fn wr(gpa: std.mem.Allocator, dir: []const u8, name: []const u8, body: []const u8) !void {
            const p = try std.fmt.allocPrintSentinel(gpa, "{s}/{s}", .{ dir, name }, 0);
            defer gpa.free(p);
            const rc = linux.open(p.ptr, .{ .ACCMODE = .WRONLY, .CREAT = true, .TRUNC = true, .CLOEXEC = true }, 0o644);
            try std.testing.expectEqual(linux.E.SUCCESS, linux.errno(rc));
            const fd: linux.fd_t = @intCast(rc);
            defer _ = linux.close(fd);
            try std.testing.expectEqual(body.len, linux.write(fd, body.ptr, body.len));
        }
    };
    try W.wr(alloc, root, "pyproject.toml", "[tool.pytest.ini_options]\npython_files = \"tests.py\"\n");
    try W.wr(alloc, root, "tests.py", "def test_a():\n    pass\n");
    try W.wr(alloc, root, "test_dataclasses.py", "def test_b():\n    pass\n");
    var out: std.ArrayListUnmanaged([]u8) = .empty;
    defer {
        for (out.items) |f| alloc.free(f);
        out.deinit(alloc);
    }
    try discoverTestFiles(alloc, root[0..root.len], &out);
    try std.testing.expectEqual(@as(usize, 1), out.items.len);
    try std.testing.expect(std.mem.endsWith(u8, out.items[0], "/tests.py"));
    inline for (.{ "pyproject.toml", "tests.py", "test_dataclasses.py" }) |nm| {
        const p = try std.fmt.allocPrintSentinel(alloc, "{s}/{s}", .{ root, nm }, 0);
        defer alloc.free(p);
        _ = linux.unlink(p.ptr);
    }
    _ = linux.rmdir(root.ptr);
}

test "custom-collector dirs emit non-.py files" {
    const alloc = std.testing.allocator;
    const pid = linux.getpid();
    const root = try std.fmt.allocPrintSentinel(alloc, "/tmp/ztest_collector_{d}", .{pid}, 0);
    defer alloc.free(root);
    _ = linux.rmdir(root.ptr);
    try std.testing.expectEqual(linux.E.SUCCESS, linux.errno(linux.mkdir(root.ptr, 0o755)));
    const data_dir = try std.fmt.allocPrintSentinel(alloc, "{s}/data", .{root}, 0);
    defer alloc.free(data_dir);
    try std.testing.expectEqual(linux.E.SUCCESS, linux.errno(linux.mkdir(data_dir.ptr, 0o755)));
    const W = struct {
        fn wr(gpa: std.mem.Allocator, dir: []const u8, name: []const u8, body: []const u8) !void {
            const p = try std.fmt.allocPrintSentinel(gpa, "{s}/{s}", .{ dir, name }, 0);
            defer gpa.free(p);
            const rc = linux.open(p.ptr, .{ .ACCMODE = .WRONLY, .CREAT = true, .TRUNC = true, .CLOEXEC = true }, 0o644);
            try std.testing.expectEqual(linux.E.SUCCESS, linux.errno(rc));
            const fd: linux.fd_t = @intCast(rc);
            defer _ = linux.close(fd);
            try std.testing.expectEqual(body.len, linux.write(fd, body.ptr, body.len));
        }
    };
    // data/ declares the hook -> alpha.txt is emitted; conftest.py itself is not (.py)
    try W.wr(alloc, data_dir[0..data_dir.len], "conftest.py", "def pytest_collect_file(parent, file_path):\n    return None\n");
    try W.wr(alloc, data_dir[0..data_dir.len], "alpha.txt", "ok\n");
    try W.wr(alloc, root[0..root.len], "test_regular.py", "def test_regular():\n    pass\n");
    var out: std.ArrayListUnmanaged([]u8) = .empty;
    defer {
        for (out.items) |f| alloc.free(f);
        out.deinit(alloc);
    }
    try discoverTestFiles(alloc, root[0..root.len], &out);
    var saw_txt = false;
    var saw_py = false;
    for (out.items) |p| {
        if (std.mem.endsWith(u8, p, "alpha.txt")) saw_txt = true;
        if (std.mem.endsWith(u8, p, "test_regular.py")) saw_py = true;
    }
    try std.testing.expect(saw_txt);
    try std.testing.expect(saw_py);
    inline for (.{ "data/conftest.py", "data/alpha.txt", "test_regular.py" }) |nm| {
        const p = try std.fmt.allocPrintSentinel(alloc, "{s}/{s}", .{ root, nm }, 0);
        defer alloc.free(p);
        _ = linux.unlink(p.ptr);
    }
    _ = linux.rmdir(data_dir.ptr);
    _ = linux.rmdir(root.ptr);
}

test "crash accounting folds the per-test frames and keeps the collected count" {
    // A worker that dies without FILE_DONE (os._exit, SIGKILL, timeout) leaves
    // only its per-test frames behind. Those frames are the verified record of
    // what finished, so they replace the counts the missing FILE_DONE would
    // have reported — a crash must not erase tests that already passed.
    const alloc = std.testing.allocator;

    var summary: FileRunSummary = .{};
    _ = try parseAndApplyFrameJson(alloc, null, "{\"t\":\"FILE_START\",\"file\":\"t.py\"}", &summary, true, false, false);
    _ = try parseAndApplyFrameJson(alloc, null, "{\"t\":\"COLLECTION_DONE\",\"file\":\"t.py\",\"collected\":4}", &summary, true, false, false);
    _ = try parseAndApplyFrameJson(alloc, null, "{\"t\":\"TEST_PASS\",\"nodeid\":\"t.py::a\"}", &summary, true, false, false);
    _ = try parseAndApplyFrameJson(alloc, null, "{\"t\":\"TEST_PASS\",\"nodeid\":\"t.py::b\"}", &summary, true, false, false);
    _ = try parseAndApplyFrameJson(alloc, null, "{\"t\":\"TEST_SKIP\",\"nodeid\":\"t.py::c\"}", &summary, true, false, false);
    _ = try parseAndApplyFrameJson(alloc, null, "{\"t\":\"TEST_FAIL\",\"nodeid\":\"t.py::d\"}", &summary, true, false, false);
    try std.testing.expect(!summary.got_file_done);
    try std.testing.expectEqual(@as(u32, 4), summary.collected);

    foldFrameTallies(&summary);
    try std.testing.expectEqual(@as(u32, 2), summary.passed);
    try std.testing.expectEqual(@as(u32, 1), summary.failed);
    try std.testing.expectEqual(@as(u32, 1), summary.skipped);
    try std.testing.expectEqual(@as(u32, 4), summary.collected);

    // A FILE_DONE that carries no collected field must not wipe the count
    // reported by COLLECTION_DONE, and its counts stay authoritative.
    _ = try parseAndApplyFrameJson(alloc, null, "{\"t\":\"FILE_DONE\",\"passed\":4,\"failed\":0,\"skipped\":0,\"deselected\":0,\"status\":\"PASSED\"}", &summary, true, false, false);
    try std.testing.expectEqual(@as(u32, 4), summary.collected);
    try std.testing.expectEqual(@as(u32, 4), summary.passed);

    // A collect-only FILE_DONE still publishes the selection size itself.
    var co: FileRunSummary = .{};
    _ = try parseAndApplyFrameJson(alloc, null, "{\"t\":\"COLLECTION_DONE\",\"file\":\"t.py\",\"collected\":3}", &co, true, false, true);
    _ = try parseAndApplyFrameJson(alloc, null, "{\"t\":\"FILE_DONE\",\"passed\":0,\"failed\":0,\"skipped\":0,\"deselected\":2,\"collected\":3,\"status\":\"COLLECTED\"}", &co, true, false, true);
    try std.testing.expectEqual(@as(u32, 3), co.collected);

    // Folding is a no-op for a file that reported FILE_DONE, even when the
    // frames disagree with it (teardown-only failures are FILE_DONE's truth).
    var done: FileRunSummary = .{ .got_file_done = true, .passed = 1, .failed = 1, .frame_passed = 9 };
    foldFrameTallies(&done);
    try std.testing.expectEqual(@as(u32, 1), done.passed);
    try std.testing.expectEqual(@as(u32, 1), done.failed);

    // The human short summary is a bounded ring, and bounded storage has to be
    // honest about it. Dedup identity comes from the FULL nodeid (128-bit hash +
    // length), display truncation is marked, and failures past the cap are
    // counted instead of vanishing.
    defer resetFailedTestNotes();

    resetFailedTestNotes();
    var long_a: [SUMMARY_NODEID_MAX + 8]u8 = undefined;
    @memset(long_a[0..], 'a');
    var long_b = long_a;
    long_b[long_b.len - 1] = 'b'; // same 512-byte display prefix, different tail
    recordFailedTest(long_a[0..], "first");
    recordFailedTest(long_b[0..], "second");
    try std.testing.expectEqual(@as(usize, 2), failedTestNoteCount());
    const noted = failedTestNotes();
    try std.testing.expectEqual(@as(usize, SUMMARY_NODEID_MAX), noted[0].nodeid.len);
    try std.testing.expect(std.mem.endsWith(u8, noted[0].nodeid, "…"));
    try std.testing.expect(!std.mem.eql(u8, noted[0].nodeid, long_a[0..]));
    recordFailedTest(long_a[0..], "re-reported by teardown");
    try std.testing.expectEqual(@as(usize, 2), failedTestNoteCount());

    // Oversized reasons are truncated with the same visible marker.
    resetFailedTestNotes();
    var long_reason: [SUMMARY_REASON_MAX + 10]u8 = undefined;
    @memset(long_reason[0..], 'r');
    recordFailedTest("t.py::reason", long_reason[0..]);
    try std.testing.expectEqual(@as(usize, SUMMARY_REASON_MAX), failedTestNotes()[0].reason.len);
    try std.testing.expect(std.mem.endsWith(u8, failedTestNotes()[0].reason, "…"));

    // Fill the ring, then prove overflow is counted per distinct nodeid.
    resetFailedTestNotes();
    var id_buf: [32]u8 = undefined;
    var n: usize = 0;
    while (n < MAX_SUMMARY_FAILS) : (n += 1) {
        const nid = std.fmt.bufPrint(&id_buf, "t.py::case{d}", .{n}) catch unreachable;
        recordFailedTest(nid, "boom");
    }
    try std.testing.expectEqual(MAX_SUMMARY_FAILS, failedTestNoteCount());
    try std.testing.expectEqual(@as(u64, 0), failedTestOmittedReports());
    recordFailedTest("t.py::overflow-1", "boom");
    recordFailedTest("t.py::overflow-2", "boom");
    // An unlisted failure re-reported is another *record*: the block says
    // "reports not shown", so it must not pretend to know unique counts.
    recordFailedTest("t.py::overflow-2", "boom");
    try std.testing.expectEqual(@as(u64, 3), failedTestOmittedReports());
    try std.testing.expectEqual(MAX_SUMMARY_FAILS, failedTestNoteCount());
    // A repeat of a failure that IS listed still collapses onto its first line.
    recordFailedTest("t.py::case0", "boom again");
    try std.testing.expectEqual(@as(u64, 3), failedTestOmittedReports());
    try std.testing.expectEqualStrings("boom", failedTestNotes()[0].reason);
}

test "interrupt tallies mirror a completed session summary" {
    g_partial = .{};
    defer g_partial = .{};

    // A clean file with two passes.
    notePartialTally(.{ .passed = 2, .got_file_done = true }, .passed);
    try std.testing.expectEqual(@as(u32, 1), g_partial.files);
    try std.testing.expectEqual(@as(u32, 2), g_partial.passed);
    try std.testing.expectEqual(@as(u32, 0), g_partial.failed);

    // A plain FAILED outcome with zero failed counts still bills one failure.
    notePartialTally(.{ .passed = 1, .got_file_done = true }, .failed);
    try std.testing.expectEqual(@as(u32, 2), g_partial.files);
    try std.testing.expectEqual(@as(u32, 3), g_partial.passed);
    try std.testing.expectEqual(@as(u32, 1), g_partial.failed);

    // A crashed file: its frames stay counted, plus one failure for the crash.
    notePartialTally(.{ .passed = 3, .failed = 1, .skipped = 2 }, .crashed);
    try std.testing.expectEqual(@as(u32, 3), g_partial.files);
    try std.testing.expectEqual(@as(u32, 6), g_partial.passed);
    try std.testing.expectEqual(@as(u32, 3), g_partial.failed);
    try std.testing.expectEqual(@as(u32, 2), g_partial.skipped);
}

test "session exit code follows pytest precedence (collection error outranks failures)" {
    const ok = ExitCode.ok.toU8();
    const failed = ExitCode.tests_failed.toU8();
    const coll = ExitCode.interrupted.toU8();
    const none = ExitCode.no_tests_collected.toU8();

    try std.testing.expect(isCollectionErrorStatus("COLLECTION_ERROR"));
    try std.testing.expect(!isCollectionErrorStatus("FAILED"));
    try std.testing.expect(!isCollectionErrorStatus("PASSED"));

    // Clean session with passing tests.
    try std.testing.expectEqual(ok, sessionExitCode(false, 0, 0, 3, 0, 0));
    // Plain test failures.
    try std.testing.expectEqual(failed, sessionExitCode(false, 0, 2, 1, 0, 0));
    // A single collection error: exit 2, not 1.
    try std.testing.expectEqual(coll, sessionExitCode(false, 1, 0, 1, 0, 0));
    // Collection error plus real failures: pytest still exits 2.
    try std.testing.expectEqual(coll, sessionExitCode(false, 1, 2, 1, 0, 0));
    // Interruption keeps exit 2 on its own.
    try std.testing.expectEqual(coll, sessionExitCode(true, 0, 0, 4, 0, 0));
    // Empty session: exit 5; skipped-only and collect-only runs are not "empty".
    try std.testing.expectEqual(none, sessionExitCode(false, 0, 0, 0, 0, 0));
    try std.testing.expectEqual(ok, sessionExitCode(false, 0, 0, 0, 2, 0));
    try std.testing.expectEqual(ok, sessionExitCode(false, 0, 0, 0, 0, 7));
}

test "explicit targets are validated against disk before dispatch" {
    // pathExists follows the filesystem: the root directory exists, a random leaf does not.
    try std.testing.expect(pathExists("/"));
    try std.testing.expect(!pathExists("/ztest_missing_probe_absent"));

    // `path::nodeid` selections are validated through their path part.
    try std.testing.expectEqualStrings("tests/test_a.py", targetBasePath("tests/test_a.py::test_one"));
    try std.testing.expectEqualStrings("tests/test_a.py", targetBasePath("tests/test_a.py"));
    try std.testing.expectEqualStrings("", targetBasePath("::test_one"));

    const present = [_][]const u8{ "/", "//", "/tmp" };
    try std.testing.expectEqual(@as(?[]const u8, null), findMissingTarget(&present));

    const with_missing = [_][]const u8{ "/", "/ztest_missing_probe_absent" };
    try std.testing.expectEqualStrings("/ztest_missing_probe_absent", findMissingTarget(&with_missing).?);

    const missing_nodeid = [_][]const u8{"/ztest_missing_probe_absent.py::test_x"};
    try std.testing.expectEqualStrings("/ztest_missing_probe_absent.py::test_x", findMissingTarget(&missing_nodeid).?);

    // Empty entries are not targets and never fail validation.
    const empty = [_][]const u8{""};
    try std.testing.expectEqual(@as(?[]const u8, null), findMissingTarget(&empty));
}

test "explicit interpreter is validated before any spawn" {
    // Directories carry execute bits but are not interpreters.
    try std.testing.expect(pathExists("/tmp"));
    try std.testing.expect(!isExecutableFile("/tmp"));
    try std.testing.expect(!isExecutableFile("/ztest_missing_probe_absent"));
    try std.testing.expect(!pathExists(""));

    try std.testing.expect(!interpreterAvailable("/nonexistent/python9", null));
    try std.testing.expect(!interpreterAvailable("", null));
    // A bare name resolves through PATH only when a usable entry exists there.
    const sh_on_path = isExecutableFile("/bin/sh") or isExecutableFile("/usr/bin/sh");
    try std.testing.expectEqual(sh_on_path, interpreterAvailable("sh", "/bin:/usr/bin"));
    try std.testing.expect(!interpreterAvailable("sh", "/ztest_missing_probe_absent"));
    try std.testing.expect(!interpreterAvailable("sh", null));
    try std.testing.expect(!interpreterAvailable("/bin/sh", null) == !isExecutableFile("/bin/sh"));
}
