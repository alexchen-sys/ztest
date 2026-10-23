/// exec spawn with pdeathsig arming of the child branch before execve.
///
/// std.process.spawn in Zig 0.17 has no pre-exec hook, so the zygote and
/// clean-spawn workers of the supervisor are spawned through the direct
/// fork→(arm pdeathsig)→execve pair.
/// prctl(PR_SET_PDEATHSIG, SIGKILL) survives execve (except setugid) and
/// is reset in the child after fork — the supervisor arms it itself, no ctypes in the shim.
/// getppid verification right after arming: a parent that already died
/// produces no retrospective signal.
const std = @import("std");
const linux = std.os.linux;
const process = std.process;
const Allocator = std.mem.Allocator;

const SpawnArmError = process.SpawnError || Allocator.Error;

/// Bounded reap for spawn-handshake error paths — blocking wait4 is
/// forbidden (a SIGKILLed child dies fast; a hung one must not hang the supervisor).
fn reapChildBoundedLocal(pid: i32, budget_ms: u32) void {
    var st: i32 = 0;
    var waited: u32 = 0;
    while (waited < budget_ms) : (waited += 1) {
        const rc = linux.wait4(pid, &st, linux.W.NOHANG, null);
        if (linux.errno(rc) != .SUCCESS) return; // ECHILD: already reaped
        if (rc == pid) return;
        const ts = linux.timespec{ .sec = 0, .nsec = 1_000_000 };
        _ = linux.nanosleep(&ts, null);
    }
}

fn armPdeathsigChild(parent_pid: i32) void {
    // The prctl result is checked — a child without pdeathsig is dangerous (fail-closed).
    const prc = linux.prctl(@backingInt(linux.PR.SET_PDEATHSIG), @backingInt(linux.SIG.KILL), 0, 0, 0);
    if (linux.errno(prc) != .SUCCESS) {
        linux.exit_group(125);
    }
    if (@as(i32, @intCast(linux.getppid())) != parent_pid) {
        // The parent already died between fork and arming: no retrospective signal —
        // exit immediately (the CLOEXEC err pipe closes itself, the parent sees EOF).
        linux.exit_group(125);
    }
}

/// fork + PR_SET_PDEATHSIG(SIGKILL) + execve. `environ_map` (optional) builds
/// the env block; stdio: stdin → /dev/null, stdout/stderr are inherited. Every fd except
/// 0/1/2 must be CLOEXEC at the call site (the supervisor's contract: slots, the ctrl
/// socket and conn sockets are created CLOEXEC). PATH resolution of argv[0] happens before fork.
/// Returns the child's pid; exec failure is detected via the err pipe (CLOEXEC → EOF = success).
pub fn spawnArmed(
    gpa: Allocator,
    argv: []const []const u8,
    environ_map: ?*const process.Environ.Map,
) SpawnArmError!i32 {
    var arena_inst = std.heap.ArenaAllocator.init(gpa);
    defer arena_inst.deinit();
    const arena = arena_inst.allocator();

    // argv[0] resolution before fork (no allocations between fork and execve).
    const exe_path: []const u8 = if (std.mem.indexOfScalar(u8, argv[0], '/') != null)
        argv[0]
    else blk: {
        const PATH: []const u8 = if (environ_map) |em| (em.get("PATH") orelse "/usr/local/bin:/usr/bin:/bin") else "/usr/local/bin:/usr/bin:/bin";
        var pit = std.mem.tokenizeScalar(u8, PATH, ':');
        while (pit.next()) |dir| {
            const cand = try std.fmt.allocPrint(arena, "{s}/{s}", .{ dir, argv[0] });
            const cand_z = try arena.dupeSentinel(u8, cand, 0);
            if (linux.access(cand_z, linux.X_OK) == 0) break :blk cand;
        }
        break :blk argv[0]; // the real errno from execve itself
    };
    const exe_z = try arena.dupeSentinel(u8, exe_path, 0);

    // Null-terminated argv.
    const argv_buf = try arena.allocSentinel(?[*:0]const u8, argv.len, null);
    for (argv, 0..) |arg, i| argv_buf[i] = (try arena.dupeSentinel(u8, arg, 0)).ptr;

    // Env block (allocated before fork). Without environ_map we inherit the current
    // environ via Io.Threaded.global_single_threaded.environ (the global startup
    // block installed before main) → createPosixBlock (a deep copy into the arena).
    const env_block: process.Environ.PosixBlock = if (environ_map) |em|
        try em.createPosixBlock(arena, .{})
    else blk: {
        const tio = std.Io.Threaded.global_single_threaded;
        var env_map = try process.Environ.createMap(.{ .block = tio.environ.process_environ.block }, arena);
        break :blk try env_map.createPosixBlock(arena, .{});
    };

    // err pipe: CLOEXEC; on successful execve the write end closes automatically (EOF to the parent).
    var err_pipe: [2]linux.fd_t = undefined;
    if (linux.errno(linux.pipe2(&err_pipe, .{ .CLOEXEC = true })) != .SUCCESS) return error.SystemResources;

    const dev_null_fd = blk: {
        // CLOEXEC: dup2(dev_null_fd, 0) in the child does not inherit CLOEXEC onto the target fd 0,
        // and the original descriptor need not survive the zygote's execve.
        const rc = linux.open("/dev/null", .{ .ACCMODE = .RDWR, .CLOEXEC = true }, 0);
        if (linux.errno(rc) != .SUCCESS) {
            _ = linux.close(err_pipe[0]);
            _ = linux.close(err_pipe[1]);
            return error.NoDevice;
        }
        break :blk @as(linux.fd_t, @intCast(rc));
    };
    defer _ = linux.close(dev_null_fd);

    // The parent's identity is captured before fork — a post-fork getppid()
    // after the parent's death would return 1/subreaper, and the arming check
    // would compare against the wrong target. The child inherits parent_pid
    // through the address space.
    const parent_pid: i32 = @intCast(linux.getpid());
    const fork_rc = linux.fork();
    if (linux.errno(fork_rc) != .SUCCESS) {
        _ = linux.close(err_pipe[0]);
        _ = linux.close(err_pipe[1]);
        return error.SystemResources;
    }
    if (fork_rc == 0) {
        // ---- child: arming before execve ----
        _ = linux.close(err_pipe[0]);
        armPdeathsigChild(parent_pid);
        if (linux.dup2(dev_null_fd, 0) < 0) linux.exit_group(126);
        const argv_ptr: [*:null]const ?[*:0]const u8 = @ptrCast(argv_buf.ptr);
        const env_ptr: [*:null]const ?[*:0]const u8 = @ptrCast(env_block.slice.ptr);
        const exec_rc = linux.execve(exe_z.ptr, argv_ptr, env_ptr);
        // execve failed: report errno to the parent and exit.
        const code: u32 = @as(u16, @backingInt(linux.errno(exec_rc)));
        _ = linux.write(err_pipe[1], std.mem.asBytes(&code).ptr, 4);
        linux.exit_group(127);
    }
    // ---- parent ----
    const pid: i32 = @intCast(fork_rc);
    _ = linux.close(err_pipe[1]);
    var code: u32 = 0;
    var got: usize = 0;
    // The handshake is deadline-bounded. The child must either complete execve
    // (CLOEXEC → EOF) or write errno and exit; a hung pre-exec child
    // (SIGSTOP/debugger) does not hang the supervisor on a blocking read.
    var hs_pfd = [_]linux.pollfd{.{ .fd = err_pipe[0], .events = linux.POLL.IN, .revents = 0 }};
    const poll_rc = linux.poll(&hs_pfd, 1, 10_000);
    if (linux.errno(poll_rc) != .SUCCESS or poll_rc == 0) {
        _ = linux.kill(pid, linux.SIG.KILL);
        reapChildBoundedLocal(pid, 2_000);
        _ = linux.close(err_pipe[0]);
        return error.SystemResources;
    }
    while (got < 4) {
        const rrc = linux.read(err_pipe[0], std.mem.asBytes(&code).ptr + got, 4 - got);
        // EINTR is neither EOF nor data: without a retry a signal race would
        // masquerade a real exec failure as a "successful" spawn.
        if (linux.errno(rrc) == .INTR) continue;
        if (linux.errno(rrc) != .SUCCESS) break;
        if (rrc == 0) break; // EOF: CLOEXEC closed the write end — execve succeeded
        got += @intCast(rrc);
    }
    _ = linux.close(err_pipe[0]);
    if (got >= 4) {
        // The child died before/at execve: reap the zombie (bounded) and return an error.
        reapChildBoundedLocal(pid, 2_000);
        return error.FileNotFound;
    }
    return pid;
}

test "spawnArmed: pdeathsig is armed on the exec'd child and survives exec" {
    // Live integration test: spawn python3; the child reads its own
    // pdeathsig via PR_GET_PDEATHSIG (prctl(2)) — the value must be 9 (SIGKILL):
    // the arming happened in the child branch before execve and survived exec.
    // Second invariant: the child's getppid() == the test parent's pid
    // (the arming check passed).
    const self_pid: i32 = @intCast(linux.getpid());
    var script_buf: [256]u8 = undefined;
    const script = try std.fmt.bufPrint(&script_buf, "import ctypes, os\nlibc = ctypes.CDLL(None, use_errno=True)\nv = ctypes.c_int(0)\nrc = libc.prctl(2, ctypes.byref(v), 0, 0, 0)\nassert rc == 0, rc\nassert v.value == 9, v.value\nassert os.getppid() == {d}\nprint('OK')\n", .{self_pid});
    const argv = [_][]const u8{ "python3", "-c", script };
    const pid = try spawnArmed(std.testing.allocator, &argv, null);
    var st: i32 = 0;
    const wrc = linux.wait4(pid, &st, 0, null);
    try std.testing.expect(linux.errno(wrc) == .SUCCESS);
    const stu: u32 = @bitCast(st);
    try std.testing.expect(linux.W.IFEXITED(stu));
    try std.testing.expectEqual(@as(u32, 0), linux.W.EXITSTATUS(stu));
}

test "spawnArmed: exec failure is reported, zombie reaped" {
    const argv = [_][]const u8{"/nonexistent/binary/definitely_missing"};
    const result = spawnArmed(std.testing.allocator, &argv, null);
    try std.testing.expectError(error.FileNotFound, result);
}
