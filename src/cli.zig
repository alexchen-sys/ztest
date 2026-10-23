const std = @import("std");
const linux = std.os.linux;

fn warnSystemInterpreterFallback(path: []const u8) void {
    var buf: [512]u8 = undefined;
    const line = std.fmt.bufPrint(&buf, "ztest: no virtualenv found (checked --python, $VIRTUAL_ENV, ./.venv, ./venv); using system interpreter {s}\n", .{path}) catch return;
    _ = linux.write(2, line.ptr, line.len);
}

/// Interpreter resolution order: --python → $VIRTUAL_ENV/bin/python (the
/// standard marker of an activated virtualenv, honored only when the file
/// exists) → ./.venv/bin/python → ./venv/bin/python → the first python3 found
/// on $PATH → /usr/bin/python3 → python3 (literal, left for execvp to
/// resolve). A run started from an activated virtualenv outside ./.venv (e.g.
/// /tmp/.../v) must use that interpreter — silently falling back to the
/// system python loses every test file to import errors.
///
/// The $PATH walk is what keeps pyenv/conda/mise toolchains working: a
/// python3 that resolves outside /usr/bin is the user's deliberate
/// interpreter and is used without comment. Landing on /usr/bin/python3
/// itself — whether reached through $PATH or through the direct fallback
/// below — emits a one-line stderr warning, because the system python usually
/// lacks the project's dependencies and that substitution must never be
/// silent.
pub fn findPythonBinary(
    io: std.Io,
    explicit_py: ?[]const u8,
    venv_env: ?[]const u8,
    path_env: ?[]const u8,
    venv_buf: []u8,
    path_buf: []u8,
) []const u8 {
    if (explicit_py) |p| return p;
    if (venv_env) |venv| {
        if (venv.len != 0) {
            if (std.fmt.bufPrint(venv_buf, "{s}/bin/python", .{venv})) |cand| {
                if (std.Io.Dir.cwd().statFile(io, cand, .{})) |_| {
                    return cand;
                } else |_| {}
            } else |_| {}
        }
    }
    const venv_candidates = [_][]const u8{
        ".venv/bin/python",
        "venv/bin/python",
    };
    for (venv_candidates) |cand| {
        if (std.Io.Dir.cwd().statFile(io, cand, .{})) |_| {
            return cand;
        } else |_| {}
    }
    // $PATH walk: the first directory holding a python3 wins. Empty
    // components are skipped — in shell semantics they mean the cwd, and a
    // test supervisor silently adopting ./python3 would be a trap, not a
    // toolchain. A candidate that is /usr/bin/python3 is the system
    // interpreter reached through $PATH: still used, but warned about.
    if (path_env) |path_val| {
        var it = std.mem.splitScalar(u8, path_val, ':');
        while (it.next()) |dir| {
            if (dir.len == 0) continue;
            const cand = std.fmt.bufPrint(path_buf, "{s}/python3", .{dir}) catch continue;
            if (std.Io.Dir.cwd().statFile(io, cand, .{})) |_| {
                if (std.mem.eql(u8, cand, "/usr/bin/python3")) {
                    warnSystemInterpreterFallback(cand);
                }
                return cand;
            } else |_| {}
        }
    }
    // Empty or python3-less $PATH: the system interpreter is the last
    // existing candidate.
    if (std.Io.Dir.cwd().statFile(io, "/usr/bin/python3", .{})) |_| {
        warnSystemInterpreterFallback("/usr/bin/python3");
        return "/usr/bin/python3";
    } else |_| {}
    warnSystemInterpreterFallback("python3");
    return "python3";
}
pub fn timeoutMicros(timeout_sec: f64) i64 {
    if (!std.math.isFinite(timeout_sec) or !(timeout_sec > 0.0)) return 0;
    // Any accepted positive value must reach the worker as a live timer.
    // @round of a sub-microsecond value (e.g. 1e-7 -> 0.1 -> 0) must not become 0:
    // 0 would silently disable the shim's per-test itimer while the supervisor still
    // enforces its own slack — an over-budget test could pass. Clamp to 1 microsecond
    // instead (an accepted timeout is never turned off, only made stricter).
    const us_rounded = @round(timeout_sec * 1_000_000.0);
    if (us_rounded > 86_400_000_000.0) return 0;
    if (us_rounded < 1.0) return 1;
    return @intFromFloat(us_rounded);
}
pub fn workerReadTimeoutMs(timeout_sec: f64) i64 {
    if (!std.math.isFinite(timeout_sec) or !(timeout_sec > 0.0)) return 3_600_000;
    return @intFromFloat(@min(86_401_500.0, @max(1500.0, timeout_sec * 1000.0) + 1500.0));
}
fn parsePositiveTimeout(s: []const u8) ?f64 {
    const tv = std.fmt.parseFloat(f64, s) catch return null;
    if (!std.math.isFinite(tv) or tv <= 0.0 or tv > 86400.0) return null;
    return tv;
}
fn parseJobsCount(s: []const u8) ?usize {
    const jv = std.fmt.parseInt(usize, s, 10) catch return null;
    if (jv == 0 or jv > 64) return null;
    return @min(jv, 16);
}

/// A preload list is comma-separated dotted Python module names
/// (`pydantic,yaml.CSafeLoader`); every dotted segment must be a valid
/// identifier. The shim re-checks defensively (the list can also arrive via
/// the ZTEST_PRELOAD_PKGS environment variable) — the CLI simply fails fast.
fn validPreloadList(s: []const u8) bool {
    if (s.len == 0 or s.len > 1024) return false;
    var entries = std.mem.splitScalar(u8, s, ',');
    while (entries.next()) |entry_raw| {
        const entry = std.mem.trim(u8, entry_raw, " ");
        if (entry.len == 0) return false;
        var segs = std.mem.splitScalar(u8, entry, '.');
        while (segs.next()) |seg| {
            if (seg.len == 0) return false;
            for (seg, 0..) |ch, ci| {
                const ok = (ch >= 'a' and ch <= 'z') or (ch >= 'A' and ch <= 'Z') or ch == '_' or
                    (ci > 0 and ch >= '0' and ch <= '9');
                if (!ok) return false;
            }
        }
    }
    return true;
}
fn nextFilterToken(expr: []const u8, pos: *usize) ?[]const u8 {
    while (pos.* < expr.len and (expr[pos.*] == ' ' or expr[pos.*] == '\t' or expr[pos.*] == '\r' or expr[pos.*] == '\n')) pos.* += 1;
    if (pos.* >= expr.len) return null;
    if (expr[pos.*] == '(' or expr[pos.*] == ')') {
        const tok = expr[pos.* .. pos.* + 1];
        pos.* += 1;
        return tok;
    }
    const start = pos.*;
    while (pos.* < expr.len and expr[pos.*] != ' ' and expr[pos.*] != '\t' and expr[pos.*] != '\r' and expr[pos.*] != '\n' and expr[pos.*] != '(' and expr[pos.*] != ')') pos.* += 1;
    return expr[start..pos.*];
}
fn parseFilterOr(expr: []const u8, pos: *usize) bool {
    if (!parseFilterAnd(expr, pos)) return false;
    while (true) {
        var save = pos.*;
        const tok = nextFilterToken(expr, &save) orelse break;
        if (!std.mem.eql(u8, tok, "or")) break;
        pos.* = save;
        if (!parseFilterAnd(expr, pos)) return false;
    }
    return true;
}
fn parseFilterAnd(expr: []const u8, pos: *usize) bool {
    if (!parseFilterNot(expr, pos)) return false;
    while (true) {
        var save = pos.*;
        const tok = nextFilterToken(expr, &save) orelse break;
        if (!std.mem.eql(u8, tok, "and")) break;
        pos.* = save;
        if (!parseFilterNot(expr, pos)) return false;
    }
    return true;
}
fn parseFilterNot(expr: []const u8, pos: *usize) bool {
    const tok = nextFilterToken(expr, pos) orelse return false;
    if (std.mem.eql(u8, tok, "not")) return parseFilterNot(expr, pos);
    if (std.mem.eql(u8, tok, "(")) {
        if (!parseFilterOr(expr, pos)) return false;
        const close = nextFilterToken(expr, pos) orelse return false;
        return std.mem.eql(u8, close, ")");
    }
    if (std.mem.eql(u8, tok, ")") or std.mem.eql(u8, tok, "and") or std.mem.eql(u8, tok, "or")) return false;
    return true;
}
pub fn validFilterExpr(expr: []const u8) bool {
    const trimmed = std.mem.trim(u8, expr, " \t\r\n");
    if (trimmed.len == 0) return true;
    var pos: usize = 0;
    if (!parseFilterOr(trimmed, &pos)) return false;
    return nextFilterToken(trimmed, &pos) == null;
}
pub const ParsedCli = struct {
    k_expr: []const u8 = "",
    m_expr: []const u8 = "",
    maxfail: u32 = 0,
    jobs: usize = 4,
    timeout_sec: f64 = 0.0,
    llm_mode: bool = false,
    verbose: bool = false,
    quiet: bool = false,
    no_capture: bool = false,
    collect_only: bool = false,
    exec_mode: bool = false,
    /// lazy-zygote: true iff the user passed `--mode=...` explicitly. Kept separate from exec_mode so the
    /// automatic lazy-zygote rule (single-file suite => clean-spawn) never overrides an explicit
    /// `--mode=zygote` request.
    mode_explicit: bool = false,
    /// Preload list from `--preload a,b,c` (comma-separated module names). Empty
    /// by default: the zygote preloads stdlib modules only; any listed module is
    /// forwarded to the zygote env as ZTEST_PRELOAD_PKGS.
    preload_pkgs: []const u8 = "",
    /// Execution engine inside the supervised worker: `false` (default) uses
    /// the zero-dependency native shim; `true` (`--engine=pytest`) delegates
    /// collection and execution inside each isolated worker to upstream pytest
    /// with full pluggy plugin compatibility while keeping the Zig supervisor,
    /// zygote fork server, pidfd hardware timeouts, and ZTST binary IPC.
    engine_pytest: bool = false,
    engine_explicit: bool = false,
    show_help: bool = false,
    show_version: bool = false,
    explicit_py: ?[]const u8 = null,
};
pub fn parseCliArgs(
    gpa: std.mem.Allocator,
    args: []const []const u8,
    targets: *std.ArrayListUnmanaged([]const u8),
) error{ OutOfMemory, UsageError }!ParsedCli {
    var cli: ParsedCli = .{};
    var i: usize = 1;
    var positional_only = false;

    while (i < args.len) : (i += 1) {
        const arg = args[i];
        if (positional_only) {
            try targets.append(gpa, arg);
            continue;
        }
        if (std.mem.eql(u8, arg, "--")) {
            positional_only = true;
            continue;
        }
        if (std.mem.eql(u8, arg, "--help") or std.mem.eql(u8, arg, "-h")) {
            cli.show_help = true;
            return cli;
        } else if (std.mem.eql(u8, arg, "--version") or std.mem.eql(u8, arg, "-V")) {
            cli.show_version = true;
            return cli;
        } else if (std.mem.eql(u8, arg, "--llm")) {
            cli.llm_mode = true;
        } else if (std.mem.eql(u8, arg, "-v") or std.mem.eql(u8, arg, "-vv") or std.mem.eql(u8, arg, "--verbose")) {
            cli.verbose = true;
        } else if (std.mem.eql(u8, arg, "-q") or std.mem.eql(u8, arg, "-qq") or std.mem.eql(u8, arg, "--quiet")) {
            cli.quiet = true;
        } else if (std.mem.eql(u8, arg, "-s") or std.mem.eql(u8, arg, "--no-capture") or std.mem.eql(u8, arg, "--capture=no")) {
            cli.no_capture = true;
        } else if (std.mem.eql(u8, arg, "--collect-only") or std.mem.eql(u8, arg, "--co")) {
            cli.collect_only = true;
        } else if (std.mem.eql(u8, arg, "-x") or std.mem.eql(u8, arg, "--exitfirst")) {
            cli.maxfail = 1;
        } else if (std.mem.startsWith(u8, arg, "--maxfail=")) {
            const mf = std.fmt.parseInt(u32, arg["--maxfail=".len..], 10) catch return error.UsageError;
            if (mf == 0) return error.UsageError;
            cli.maxfail = mf;
        } else if (std.mem.eql(u8, arg, "--maxfail")) {
            if (i + 1 >= args.len) return error.UsageError;
            i += 1;
            const mf = std.fmt.parseInt(u32, args[i], 10) catch return error.UsageError;
            if (mf == 0) return error.UsageError;
            cli.maxfail = mf;
        } else if (std.mem.startsWith(u8, arg, "--timeout=")) {
            cli.timeout_sec = parsePositiveTimeout(arg["--timeout=".len..]) orelse return error.UsageError;
        } else if (std.mem.eql(u8, arg, "--timeout")) {
            if (i + 1 >= args.len) return error.UsageError;
            i += 1;
            cli.timeout_sec = parsePositiveTimeout(args[i]) orelse return error.UsageError;
        } else if (std.mem.eql(u8, arg, "--mode=exec")) {
            cli.exec_mode = true;
            cli.mode_explicit = true;
        } else if (std.mem.eql(u8, arg, "--mode=zygote")) {
            cli.exec_mode = false;
            cli.mode_explicit = true;
        } else if (std.mem.eql(u8, arg, "--mode")) {
            if (i + 1 >= args.len) return error.UsageError;
            i += 1;
            if (std.mem.eql(u8, args[i], "exec")) {
                cli.exec_mode = true;
                cli.mode_explicit = true;
            } else if (std.mem.eql(u8, args[i], "zygote")) {
                cli.exec_mode = false;
                cli.mode_explicit = true;
            } else {
                return error.UsageError;
            }
        } else if (std.mem.eql(u8, arg, "--engine=native")) {
            cli.engine_pytest = false;
            cli.engine_explicit = true;
        } else if (std.mem.eql(u8, arg, "--engine=pytest")) {
            cli.engine_pytest = true;
            cli.engine_explicit = true;
        } else if (std.mem.startsWith(u8, arg, "--engine=")) {
            return error.UsageError;
        } else if (std.mem.eql(u8, arg, "--engine")) {
            if (i + 1 >= args.len) return error.UsageError;
            i += 1;
            if (std.mem.eql(u8, args[i], "native")) {
                cli.engine_pytest = false;
                cli.engine_explicit = true;
            } else if (std.mem.eql(u8, args[i], "pytest")) {
                cli.engine_pytest = true;
                cli.engine_explicit = true;
            } else {
                return error.UsageError;
            }
        } else if (std.mem.startsWith(u8, arg, "--python=")) {
            const py = arg["--python=".len..];
            if (py.len == 0) return error.UsageError;
            cli.explicit_py = py;
        } else if (std.mem.eql(u8, arg, "--python")) {
            if (i + 1 >= args.len) return error.UsageError;
            i += 1;
            if (args[i].len == 0) return error.UsageError;
            cli.explicit_py = args[i];
        } else if (std.mem.startsWith(u8, arg, "--preload=")) {
            const pl = arg["--preload=".len..];
            if (!validPreloadList(pl)) return error.UsageError;
            cli.preload_pkgs = pl;
        } else if (std.mem.eql(u8, arg, "--preload")) {
            if (i + 1 >= args.len) return error.UsageError;
            i += 1;
            if (!validPreloadList(args[i])) return error.UsageError;
            cli.preload_pkgs = args[i];
        } else if (std.mem.startsWith(u8, arg, "-k=")) {
            const ke = arg["-k=".len..];
            if (!validFilterExpr(ke)) return error.UsageError;
            cli.k_expr = ke;
        } else if (std.mem.eql(u8, arg, "-k")) {
            if (i + 1 >= args.len) return error.UsageError;
            i += 1;
            if (!validFilterExpr(args[i])) return error.UsageError;
            cli.k_expr = args[i];
        } else if (std.mem.startsWith(u8, arg, "-m=")) {
            const me = arg["-m=".len..];
            if (!validFilterExpr(me)) return error.UsageError;
            cli.m_expr = me;
        } else if (std.mem.eql(u8, arg, "-m")) {
            if (i + 1 >= args.len) return error.UsageError;
            i += 1;
            if (!validFilterExpr(args[i])) return error.UsageError;
            cli.m_expr = args[i];
        } else if (std.mem.eql(u8, arg, "-n") or std.mem.eql(u8, arg, "-j")) {
            if (i + 1 >= args.len) return error.UsageError;
            i += 1;
            cli.jobs = parseJobsCount(args[i]) orelse return error.UsageError;
        } else if (std.mem.startsWith(u8, arg, "-j=")) {
            cli.jobs = parseJobsCount(arg["-j=".len..]) orelse return error.UsageError;
        } else if (std.mem.startsWith(u8, arg, "-n=")) {
            cli.jobs = parseJobsCount(arg["-n=".len..]) orelse return error.UsageError;
        } else if (std.mem.startsWith(u8, arg, "-j") and arg.len > 2) {
            cli.jobs = parseJobsCount(arg[2..]) orelse return error.UsageError;
        } else if (std.mem.startsWith(u8, arg, "-n") and arg.len > 2) {
            cli.jobs = parseJobsCount(arg[2..]) orelse return error.UsageError;
        } else if (std.mem.startsWith(u8, arg, "--tb=") or std.mem.startsWith(u8, arg, "--color=") or std.mem.eql(u8, arg, "-ra") or std.mem.eql(u8, arg, "-rf") or std.mem.eql(u8, arg, "-rA")) {
            // Cosmetic pytest display-formatting flags accepted as compatibility no-ops
        } else if (std.mem.eql(u8, arg, "--tb")) {
            if (i + 1 >= args.len) return error.UsageError;
            i += 1;
        } else if (std.mem.startsWith(u8, arg, "-")) {
            return error.UsageError;
        } else {
            try targets.append(gpa, arg);
        }
    }
    return cli;
}

test "findPythonBinary honors $VIRTUAL_ENV, --python overrides it, a missing venv file falls through" {
    const io = std.testing.io;
    const alloc = std.testing.allocator;
    const pid = linux.getpid();
    const root = try std.fmt.allocPrintSentinel(alloc, "/tmp/ztest_venv_{d}", .{pid}, 0);
    defer alloc.free(root);
    const root_z: [*:0]const u8 = root.ptr;
    const bin_dir = try std.fmt.allocPrintSentinel(alloc, "{s}/bin", .{root}, 0);
    defer alloc.free(bin_dir);
    const py_file = try std.fmt.allocPrintSentinel(alloc, "{s}/bin/python", .{root}, 0);
    defer alloc.free(py_file);

    _ = linux.unlink(py_file.ptr);
    _ = linux.rmdir(bin_dir.ptr);
    _ = linux.rmdir(root_z);
    try std.testing.expectEqual(linux.E.SUCCESS, linux.errno(linux.mkdir(root_z, 0o755)));
    try std.testing.expectEqual(linux.E.SUCCESS, linux.errno(linux.mkdir(bin_dir.ptr, 0o755)));
    {
        const rc = linux.open(py_file.ptr, .{ .ACCMODE = .WRONLY, .CREAT = true, .TRUNC = true, .CLOEXEC = true }, 0o755);
        try std.testing.expectEqual(linux.E.SUCCESS, linux.errno(rc));
        _ = linux.close(@intCast(rc));
    }
    defer {
        _ = linux.unlink(py_file.ptr);
        _ = linux.rmdir(bin_dir.ptr);
        _ = linux.rmdir(root_z);
    }

    var buf: [4096]u8 = undefined;
    var pbuf: [4096]u8 = undefined;

    // (a) $VIRTUAL_ENV with a real bin/python wins.
    const expect_venv = try std.fmt.allocPrint(alloc, "{s}/bin/python", .{root});
    defer alloc.free(expect_venv);
    try std.testing.expectEqualStrings(expect_venv, findPythonBinary(io, null, root, null, &buf, &pbuf));

    // (b) An explicit --python beats even a valid $VIRTUAL_ENV.
    try std.testing.expectEqualStrings("/explicit/python3", findPythonBinary(io, "/explicit/python3", root, null, &buf, &pbuf));

    // (c) A $VIRTUAL_ENV whose bin/python is missing falls through to the
    // next candidate instead of returning the bogus path.
    const missing = try std.fmt.allocPrint(alloc, "{s}-missing", .{root});
    defer alloc.free(missing);
    const fb = findPythonBinary(io, null, missing, null, &buf, &pbuf);
    try std.testing.expect(!std.mem.eql(u8, fb, expect_venv));
    try std.testing.expect(!std.mem.startsWith(u8, fb, missing));

    // (d) An empty $VIRTUAL_ENV is treated as unset (never yields "/bin/python").
    const fb_empty = findPythonBinary(io, null, "", null, &buf, &pbuf);
    try std.testing.expect(!std.mem.eql(u8, fb_empty, "/bin/python"));
}

test "findPythonBinary resolves $PATH python3 before the system interpreter" {
    const io = std.testing.io;
    const alloc = std.testing.allocator;
    const pid = linux.getpid();
    const root = try std.fmt.allocPrintSentinel(alloc, "/tmp/ztest_path_{d}", .{pid}, 0);
    defer alloc.free(root);
    const root_z: [*:0]const u8 = root.ptr;
    const py_file = try std.fmt.allocPrintSentinel(alloc, "{s}/python3", .{root}, 0);
    defer alloc.free(py_file);

    _ = linux.unlink(py_file.ptr);
    _ = linux.rmdir(root_z);
    try std.testing.expectEqual(linux.E.SUCCESS, linux.errno(linux.mkdir(root_z, 0o755)));
    {
        const rc = linux.open(py_file.ptr, .{ .ACCMODE = .WRONLY, .CREAT = true, .TRUNC = true, .CLOEXEC = true }, 0o755);
        try std.testing.expectEqual(linux.E.SUCCESS, linux.errno(rc));
        _ = linux.close(@intCast(rc));
    }
    defer {
        _ = linux.unlink(py_file.ptr);
        _ = linux.rmdir(root_z);
    }

    var vbuf: [4096]u8 = undefined;
    var pbuf: [4096]u8 = undefined;

    // A python3 found on $PATH outside /usr/bin wins.
    const expect_path = try std.fmt.allocPrint(alloc, "{s}/python3", .{root});
    defer alloc.free(expect_path);
    try std.testing.expectEqualStrings(expect_path, findPythonBinary(io, null, null, root, &vbuf, &pbuf));

    // An empty $PATH component is skipped (never resolves to ./python3).
    const with_empty = try std.fmt.allocPrint(alloc, ":{s}", .{root});
    defer alloc.free(with_empty);
    try std.testing.expectEqualStrings(expect_path, findPythonBinary(io, null, null, with_empty, &vbuf, &pbuf));
}

test "--preload parses a comma-separated module list and rejects malformed values" {
    const alloc = std.testing.allocator;
    var targets: std.ArrayListUnmanaged([]const u8) = .empty;
    defer targets.deinit(alloc);

    // Both spellings land in cli.preload_pkgs.
    const eq_form = [_][]const u8{ "ztest", "--preload=pydantic,yaml", "tests" };
    var cli = try parseCliArgs(alloc, &eq_form, &targets);
    try std.testing.expectEqualStrings("pydantic,yaml", cli.preload_pkgs);
    try std.testing.expectEqual(@as(usize, 1), targets.items.len);

    targets.clearRetainingCapacity();
    const sp_form = [_][]const u8{ "ztest", "--preload", "httpx,dateutil.tz" };
    cli = try parseCliArgs(alloc, &sp_form, &targets);
    try std.testing.expectEqualStrings("httpx,dateutil.tz", cli.preload_pkgs);

    // The default stays empty: the zygote preloads stdlib modules only.
    targets.clearRetainingCapacity();
    const plain = [_][]const u8{"ztest"};
    cli = try parseCliArgs(alloc, &plain, &targets);
    try std.testing.expectEqualStrings("", cli.preload_pkgs);

    // Malformed lists fail fast with a usage error instead of reaching the zygote.
    const bad_empty = [_][]const u8{ "ztest", "--preload=" };
    try std.testing.expectError(error.UsageError, parseCliArgs(alloc, &bad_empty, &targets));
    const bad_double_comma = [_][]const u8{ "ztest", "--preload=pydantic,,yaml" };
    try std.testing.expectError(error.UsageError, parseCliArgs(alloc, &bad_double_comma, &targets));
    const bad_space = [_][]const u8{ "ztest", "--preload", "foo bar" };
    try std.testing.expectError(error.UsageError, parseCliArgs(alloc, &bad_space, &targets));
    const bad_digit = [_][]const u8{ "ztest", "--preload=9lives" };
    try std.testing.expectError(error.UsageError, parseCliArgs(alloc, &bad_digit, &targets));
    const bad_trailing = [_][]const u8{ "ztest", "--preload" };
    try std.testing.expectError(error.UsageError, parseCliArgs(alloc, &bad_trailing, &targets));

    // --engine=native|pytest and --engine native|pytest
    targets.clearRetainingCapacity();
    const eng_pytest = [_][]const u8{ "ztest", "--engine=pytest", "tests" };
    cli = try parseCliArgs(alloc, &eng_pytest, &targets);
    try std.testing.expect(cli.engine_pytest);
    try std.testing.expect(cli.engine_explicit);

    targets.clearRetainingCapacity();
    const eng_native = [_][]const u8{ "ztest", "--engine", "native", "tests" };
    cli = try parseCliArgs(alloc, &eng_native, &targets);
    try std.testing.expect(!cli.engine_pytest);
    try std.testing.expect(cli.engine_explicit);

    const bad_eng = [_][]const u8{ "ztest", "--engine=unknown" };
    try std.testing.expectError(error.UsageError, parseCliArgs(alloc, &bad_eng, &targets));
    const bad_eng_trailing = [_][]const u8{ "ztest", "--engine" };
    try std.testing.expectError(error.UsageError, parseCliArgs(alloc, &bad_eng_trailing, &targets));
}
