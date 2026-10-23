const std = @import("std");
const linux = std.os.linux;

pub const PyFileScan = enum { no_section, no_key, found_key };
pub const PyFilePatterns = struct {
    pats: [256][]const u8 = undefined,
    len: usize = 0,
    overflow: bool = false,
    buf: [65536]u8 = undefined,
    buf_len: usize = 0,
};
pub fn defaultPyFilePatterns() PyFilePatterns {
    var p: PyFilePatterns = .{};
    p.pats[0] = "test_*.py";
    p.pats[1] = "*_test.py";
    p.len = 2;
    return p;
}
fn hasUnquotedChar(s: []const u8, needle: u8) bool {
    var in_sq = false;
    var in_dq = false;
    for (s) |c| {
        if (c == '\'' and !in_dq) {
            in_sq = !in_sq;
        } else if (c == '"' and !in_sq) {
            in_dq = !in_dq;
        } else if (!in_sq and !in_dq and c == needle) {
            return true;
        }
    }
    return false;
}
fn stripCommentUnquoted(s: []const u8, allow_semi: bool) []const u8 {
    var in_sq = false;
    var in_dq = false;
    for (s, 0..) |c, idx| {
        if (c == '\'' and !in_dq) in_sq = !in_sq else if (c == '"' and !in_sq) in_dq = !in_dq else if (!in_sq and !in_dq) {
            if (c == '#' or (allow_semi and c == ';')) return std.mem.trimEnd(u8, s[0..idx], " \t\r");
        }
    }
    return std.mem.trimEnd(u8, s, " \t\r");
}
fn scanIniKeyImpl(content: []const u8, section: []const u8, key: []const u8, out: *PyFilePatterns) PyFileScan {
    var in_section = false;
    var saw_section = false;
    var it = std.mem.splitScalar(u8, content, '\n');
    while (it.next()) |line_raw| {
        const line = std.mem.trim(u8, line_raw, " \t\r");
        if (line.len == 0 or line[0] == '#' or line[0] == ';') continue;
        if (line[0] == '[') {
            if (in_section) break;
            const hdr = std.mem.trim(u8, line[1 .. std.mem.indexOfScalar(u8, line, ']') orelse line.len], " \t");
            if (std.mem.eql(u8, hdr, section)) {
                in_section = true;
                saw_section = true;
            }
            continue;
        }
        if (!in_section) continue;
        if (std.mem.indexOfScalar(u8, line, '=')) |eq| {
            const k = std.mem.trim(u8, line[0..eq], " \t");
            if (std.mem.eql(u8, k, key)) {
                splitPyFileValue(stripCommentUnquoted(line[eq + 1 ..], true), out);
                while (it.next()) |cont_raw| {
                    const cont_trim = std.mem.trim(u8, cont_raw, " \t\r");
                    if (cont_trim.len == 0 or cont_trim[0] == '#' or cont_trim[0] == ';') continue;
                    if (cont_raw[0] == ' ' or cont_raw[0] == '\t') {
                        splitPyFileValue(stripCommentUnquoted(cont_trim, true), out);
                    } else break;
                }
                return .found_key;
            }
        }
    }
    return if (saw_section) .no_key else .no_section;
}
pub fn scanIniNorecursedirs(content: []const u8, section: []const u8, out: *PyFilePatterns) PyFileScan {
    return scanIniKeyImpl(content, section, "norecursedirs", out);
}
fn scanTomlKeyImpl(content: []const u8, key: []const u8, dotted: []const u8, out: *PyFilePatterns) PyFileScan {
    var in_opts = false;
    var in_pytest = false;
    var saw_any = false;
    var it = std.mem.splitScalar(u8, content, '\n');
    while (it.next()) |line_raw| {
        const line = std.mem.trim(u8, line_raw, " \t\r");
        if (line.len == 0 or line[0] == '#') continue;
        if (line[0] == '[') {
            if (in_opts or in_pytest) {
                in_opts = false;
                in_pytest = false;
            }
            const end = std.mem.indexOfScalar(u8, line, ']') orelse line.len;
            const hdr = std.mem.trim(u8, line[1..end], " \t");
            if (std.mem.eql(u8, hdr, "tool.pytest.ini_options")) {
                in_opts = true;
                saw_any = true;
            } else if (std.mem.eql(u8, hdr, "tool.pytest")) {
                in_pytest = true;
            }
            continue;
        }
        const eq = std.mem.indexOfScalar(u8, line, '=') orelse continue;
        const k = std.mem.trim(u8, line[0..eq], " \t");
        if ((in_opts and std.mem.eql(u8, k, key)) or (in_pytest and std.mem.eql(u8, k, dotted))) {
            const rhs = stripCommentUnquoted(line[eq + 1 ..], false);
            splitPyFileValue(rhs, out);
            if (hasUnquotedChar(rhs, '[') and !hasUnquotedChar(rhs, ']')) {
                while (it.next()) |cont_raw| {
                    const cont = stripCommentUnquoted(std.mem.trim(u8, cont_raw, " \t\r"), false);
                    if (cont.len == 0) continue;
                    splitPyFileValue(cont, out);
                    if (hasUnquotedChar(cont, ']')) break;
                }
            }
            return .found_key;
        }
    }
    return if (saw_any) .no_key else .no_section;
}
pub fn scanTomlNorecursedirs(content: []const u8, out: *PyFilePatterns) PyFileScan {
    return scanTomlKeyImpl(content, "norecursedirs", "ini_options.norecursedirs", out);
}
pub fn loadNorecursedirs(root_path: []const u8, out: *PyFilePatterns) bool {
    var dir_buf: [4096]u8 = undefined;
    var dir_len: usize = 0;
    if (root_path.len > 0 and root_path[0] == '/') {
        const n = @min(root_path.len, dir_buf.len - 1);
        @memcpy(dir_buf[0..n], root_path[0..n]);
        dir_len = n;
    } else {
        var cwd_buf: [4096]u8 = @splat(0);
        const cwd_rc = linux.getcwd(&cwd_buf, cwd_buf.len);
        if (linux.errno(cwd_rc) != .SUCCESS) return false;
        const cwd_len = std.mem.indexOfScalar(u8, &cwd_buf, 0) orelse cwd_buf.len;
        const w = std.fmt.bufPrint(&dir_buf, "{s}/{s}", .{ cwd_buf[0..cwd_len], root_path }) catch return false;
        dir_len = w.len;
    }
    while (dir_len > 1 and dir_buf[dir_len - 1] == '/') dir_len -= 1;
    const as_dir = dir_buf[0..dir_len];
    if (std.mem.indexOf(u8, as_dir, "::") != null or std.mem.endsWith(u8, as_dir, ".py")) {
        if (std.mem.lastIndexOfScalar(u8, as_dir, '/')) |idx| dir_len = idx;
    }
    var hops: usize = 0;
    var cfg_buf: [262144]u8 = undefined;
    while (hops < 64) : (hops += 1) {
        const dir = dir_buf[0..dir_len];
        var pbuf: [4224]u8 = undefined;
        if (std.fmt.bufPrintSentinel(&pbuf, "{s}/pytest.ini", .{dir}, 0)) |pz| {
            var ov = false;
            if (readConfigIntoBuf(pz.ptr, cfg_buf[0..], &ov)) |n| {
                out.* = .{ .overflow = ov };
                switch (scanIniNorecursedirs(cfg_buf[0..n], "pytest", out)) {
                    .found_key => return true,
                    else => return out.overflow, // pytest.ini — rootdir; key not set
                }
            }
        } else |_| {}
        if (std.fmt.bufPrintSentinel(&pbuf, "{s}/pyproject.toml", .{dir}, 0)) |pz| {
            var ov = false;
            if (readConfigIntoBuf(pz.ptr, cfg_buf[0..], &ov)) |n| {
                out.* = .{ .overflow = ov };
                switch (scanTomlNorecursedirs(cfg_buf[0..n], out)) {
                    .found_key => return true,
                    .no_key => return out.overflow, // ini_options present — rootdir
                    .no_section => if (ov) return true,
                }
            }
        } else |_| {}
        if (std.fmt.bufPrintSentinel(&pbuf, "{s}/tox.ini", .{dir}, 0)) |pz| {
            var ov = false;
            if (readConfigIntoBuf(pz.ptr, cfg_buf[0..], &ov)) |n| {
                out.* = .{ .overflow = ov };
                switch (scanIniNorecursedirs(cfg_buf[0..n], "pytest", out)) {
                    .found_key => return true,
                    .no_key => return out.overflow,
                    .no_section => if (ov) return true,
                }
            }
        } else |_| {}
        if (std.fmt.bufPrintSentinel(&pbuf, "{s}/setup.cfg", .{dir}, 0)) |pz| {
            var ov = false;
            if (readConfigIntoBuf(pz.ptr, cfg_buf[0..], &ov)) |n| {
                out.* = .{ .overflow = ov };
                switch (scanIniNorecursedirs(cfg_buf[0..n], "tool:pytest", out)) {
                    .found_key => return true,
                    .no_key => return out.overflow,
                    .no_section => if (ov) return true,
                }
            }
        } else |_| {}
        if (dir_len <= 1) break;
        const cut = std.mem.lastIndexOfScalar(u8, dir_buf[0..dir_len], '/') orelse break;
        if (cut == 0) {
            dir_len = 1;
        } else {
            dir_len = cut;
        }
    }
    return false;
}
pub fn norecurseMatch(pats: *const PyFilePatterns, basename: []const u8, joined: []const u8) bool {
    var i: usize = 0;
    while (i < pats.len) : (i += 1) {
        const pat = pats.pats[i];
        if (pat.len == 0) continue;
        if (std.mem.indexOfScalar(u8, pat, '/') != null) {
            if (std.mem.eql(u8, joined, pat)) return true;
            if (joined.len > pat.len + 1 and
                std.mem.endsWith(u8, joined, pat) and
                joined[joined.len - pat.len - 1] == '/') return true;
        } else if (globMatch(pat, basename)) {
            return true;
        }
    }
    return false;
}
fn matchCharClass(cls_pat: []const u8, ch: u8) ?usize {
    if (cls_pat.len < 2 or cls_pat[0] != '[') return null;
    var idx: usize = 1;
    var neg = false;
    if (idx < cls_pat.len and (cls_pat[idx] == '!' or cls_pat[idx] == '^')) {
        neg = true;
        idx += 1;
    }
    var matched = false;
    var first = true;
    while (idx < cls_pat.len) {
        if (cls_pat[idx] == ']' and !first) {
            return if (matched != neg) idx + 1 else null;
        }
        first = false;
        const c1 = cls_pat[idx];
        if (idx + 2 < cls_pat.len and cls_pat[idx + 1] == '-' and cls_pat[idx + 2] != ']') {
            const c2 = cls_pat[idx + 2];
            if (ch >= c1 and ch <= c2) matched = true;
            idx += 3;
        } else {
            if (ch == c1) matched = true;
            idx += 1;
        }
    }
    return null;
}
pub fn globMatch(pat: []const u8, name: []const u8) bool {
    // Fnmatch-style glob supporting '*', '?', and '[...]' character classes.
    var pi: usize = 0;
    var ni: usize = 0;
    var star_p: ?usize = null;
    var star_n: usize = 0;
    while (ni < name.len) {
        if (pi < pat.len and pat[pi] == '*') {
            star_p = pi;
            star_n = ni;
            pi += 1;
        } else if (pi < pat.len and pat[pi] == '?') {
            pi += 1;
            ni += 1;
        } else if (pi < pat.len and pat[pi] == '[') {
            if (matchCharClass(pat[pi..], name[ni])) |adv| {
                pi += adv;
                ni += 1;
            } else if (pat[pi] == name[ni] and std.mem.indexOfScalar(u8, pat[pi + 1 ..], ']') == null) {
                pi += 1;
                ni += 1;
            } else if (star_p) |sp| {
                pi = sp + 1;
                star_n += 1;
                ni = star_n;
            } else return false;
        } else if (pi < pat.len and pat[pi] == name[ni]) {
            pi += 1;
            ni += 1;
        } else if (star_p) |sp| {
            pi = sp + 1;
            star_n += 1;
            ni = star_n;
        } else return false;
    }
    while (pi < pat.len and pat[pi] == '*') pi += 1;
    return pi == pat.len;
}
pub fn isTestFileNamePats(name: []const u8, pats: *const PyFilePatterns) bool {
    if (!std.mem.endsWith(u8, name, ".py")) return false;
    for (pats.pats[0..pats.len]) |pat| {
        if (globMatch(pat, name)) return true;
    }
    return false;
}
fn addPyFilePattern(out: *PyFilePatterns, tok_raw: []const u8) void {
    var t = std.mem.trim(u8, tok_raw, " \t\r,");
    if (t.len == 0) return;
    if (t.len >= 1 and t[0] == '[') {
        if (t.len == 1 or t[1] == '"' or t[1] == '\'' or t[1] == ' ' or t[1] == '\t') {
            t = std.mem.trimStart(u8, t[1..], " \t\r,");
        }
    }
    if (t.len >= 1 and t[t.len - 1] == ']') {
        if (t.len == 1 or t[t.len - 2] == '"' or t[t.len - 2] == '\'' or t[t.len - 2] == ' ' or t[t.len - 2] == '\t') {
            t = std.mem.trimEnd(u8, t[0 .. t.len - 1], " \t\r,");
        }
    }
    const is_dq = (t.len >= 1 and t[0] == '"');
    if (t.len >= 1 and (t[0] == '"' or t[0] == '\'')) {
        t = t[1..];
    }
    if (t.len >= 1 and (t[t.len - 1] == '"' or t[t.len - 1] == '\'')) {
        t = t[0 .. t.len - 1];
    }
    if (t.len == 0) return;
    if (out.len >= out.pats.len) {
        out.overflow = true;
        return;
    }
    const start = out.buf_len;
    var w = start;
    if (is_dq and std.mem.indexOfScalar(u8, t, '\\') != null) {
        var i: usize = 0;
        while (i < t.len) {
            if (t[i] == '\\') {
                if (i + 1 >= t.len) {
                    out.overflow = true;
                    return;
                }
                const esc = t[i + 1];
                if (esc == 'u' or esc == 'U') {
                    const hex_len: usize = if (esc == 'u') 4 else 8;
                    if (i + 2 + hex_len > t.len) {
                        out.overflow = true;
                        return;
                    }
                    const cp = std.fmt.parseInt(u21, t[i + 2 .. i + 2 + hex_len], 16) catch {
                        out.overflow = true;
                        return;
                    };
                    if (w + 4 > out.buf.len) {
                        out.overflow = true;
                        return;
                    }
                    const enc_len = std.unicode.utf8Encode(cp, out.buf[w .. w + 4]) catch {
                        out.overflow = true;
                        return;
                    };
                    w += enc_len;
                    i += 2 + hex_len;
                } else {
                    const decoded: u8 = switch (esc) {
                        '"' => '"',
                        '\\' => '\\',
                        '/' => '/',
                        't' => '\t',
                        'n' => '\n',
                        'r' => '\r',
                        'b' => 0x08,
                        'f' => 0x0c,
                        else => {
                            out.overflow = true;
                            return;
                        },
                    };
                    if (w + 1 > out.buf.len) {
                        out.overflow = true;
                        return;
                    }
                    out.buf[w] = decoded;
                    w += 1;
                    i += 2;
                }
            } else {
                if (w + 1 > out.buf.len) {
                    out.overflow = true;
                    return;
                }
                out.buf[w] = t[i];
                w += 1;
                i += 1;
            }
        }
    } else {
        if (w + t.len > out.buf.len) {
            out.overflow = true;
            return;
        }
        @memcpy(out.buf[w .. w + t.len], t);
        w += t.len;
    }
    if (w == start) return;
    out.pats[out.len] = out.buf[start..w];
    out.buf_len = w;
    out.len += 1;
}
fn splitPyFileValue(rhs: []const u8, out: *PyFilePatterns) void {
    var tok = std.mem.tokenizeAny(u8, rhs, " \t,");
    while (tok.next()) |raw| addPyFilePattern(out, raw);
}
pub fn scanIniPythonFiles(content: []const u8, section: []const u8, out: *PyFilePatterns) PyFileScan {
    return scanIniKeyImpl(content, section, "python_files", out);
}
pub fn scanTomlPythonFiles(content: []const u8, out: *PyFilePatterns) PyFileScan {
    return scanTomlKeyImpl(content, "python_files", "ini_options.python_files", out);
}
pub fn readFileIntoBuf(path_z: [*:0]const u8, buf: []u8) ?usize {
    var dummy_ov = false;
    return readConfigIntoBuf(path_z, buf, &dummy_ov);
}
pub fn readConfigIntoBuf(path_z: [*:0]const u8, buf: []u8, overflow_out: *bool) ?usize {
    const rc = linux.open(path_z, .{ .ACCMODE = .RDONLY, .CLOEXEC = true }, 0);
    if (linux.errno(rc) != .SUCCESS) return null;
    const fd: linux.fd_t = @intCast(rc);
    defer _ = linux.close(fd);
    var total: usize = 0;
    while (total < buf.len) {
        const n = linux.read(fd, buf.ptr + total, buf.len - total);
        if (linux.errno(n) != .SUCCESS) return if (total > 0) total else null;
        if (n == 0) break;
        total += n;
    }
    if (total == buf.len) {
        var extra: [1]u8 = undefined;
        const m = linux.read(fd, &extra, 1);
        if (linux.errno(m) == .SUCCESS and m > 0) overflow_out.* = true;
    }
    return total;
}
pub fn loadPythonFilePatterns(root_path: []const u8, out: *PyFilePatterns) void {
    // The pats slices point into out.buf — out lives at the caller; returning the
    // struct by value would leave the slices dangling into a dead stack frame.

    var dir_buf: [4096]u8 = undefined;
    var dir_len: usize = 0;
    if (root_path.len > 0 and root_path[0] == '/') {
        const n = @min(root_path.len, dir_buf.len - 1);
        @memcpy(dir_buf[0..n], root_path[0..n]);
        dir_len = n;
    } else {
        var cwd_buf: [4096]u8 = @splat(0);
        const cwd_rc = linux.getcwd(&cwd_buf, cwd_buf.len);
        if (linux.errno(cwd_rc) != .SUCCESS) {
            out.* = defaultPyFilePatterns();
            return;
        }
        const cwd_len = std.mem.indexOfScalar(u8, &cwd_buf, 0) orelse cwd_buf.len;
        const w = std.fmt.bufPrint(&dir_buf, "{s}/{s}", .{ cwd_buf[0..cwd_len], root_path }) catch {
            out.* = defaultPyFilePatterns();
            return;
        };
        dir_len = w.len;
    }
    while (dir_len > 1 and dir_buf[dir_len - 1] == '/') dir_len -= 1;
    const as_dir = dir_buf[0..dir_len];
    if (std.mem.indexOf(u8, as_dir, "::") != null or std.mem.endsWith(u8, as_dir, ".py")) {
        if (std.mem.lastIndexOfScalar(u8, as_dir, '/')) |idx| dir_len = idx;
    }

    var cfg_buf: [262144]u8 = undefined;
    while (true) {
        const dir = dir_buf[0..dir_len];
        var pbuf: [4224]u8 = undefined;

        if (std.fmt.bufPrintSentinel(&pbuf, "{s}/pytest.ini", .{dir}, 0)) |pz| {
            var ov = false;
            if (readConfigIntoBuf(pz.ptr, cfg_buf[0..], &ov)) |n| {
                out.* = .{ .overflow = ov };
                switch (scanIniPythonFiles(cfg_buf[0..n], "pytest", out)) {
                    .found_key => return,
                    .no_key => {
                        out.* = defaultPyFilePatterns();
                        out.overflow = ov;
                        return;
                    },
                    .no_section => {
                        out.* = defaultPyFilePatterns();
                        out.overflow = ov;
                        return;
                    }, // pytest.ini found — rootdir resolved
                }
            }
        } else |_| {}
        if (std.fmt.bufPrintSentinel(&pbuf, "{s}/pyproject.toml", .{dir}, 0)) |pz| {
            var ov = false;
            if (readConfigIntoBuf(pz.ptr, cfg_buf[0..], &ov)) |n| {
                out.* = .{ .overflow = ov };
                switch (scanTomlPythonFiles(cfg_buf[0..n], out)) {
                    .found_key => return,
                    .no_key => {
                        out.* = defaultPyFilePatterns();
                        out.overflow = ov;
                        return;
                    },
                    .no_section => if (ov) {
                        out.* = defaultPyFilePatterns();
                        out.overflow = true;
                        return;
                    }, // pyproject without ini_options — not a pytest config, keep walking
                }
            }
        } else |_| {}
        if (std.fmt.bufPrintSentinel(&pbuf, "{s}/tox.ini", .{dir}, 0)) |pz| {
            var ov = false;
            if (readConfigIntoBuf(pz.ptr, cfg_buf[0..], &ov)) |n| {
                out.* = .{ .overflow = ov };
                switch (scanIniPythonFiles(cfg_buf[0..n], "pytest", out)) {
                    .found_key => return,
                    .no_key => {
                        out.* = defaultPyFilePatterns();
                        out.overflow = ov;
                        return;
                    },
                    .no_section => {
                        out.* = defaultPyFilePatterns();
                        out.overflow = ov;
                        return;
                    },
                }
            }
        } else |_| {}
        if (std.fmt.bufPrintSentinel(&pbuf, "{s}/setup.cfg", .{dir}, 0)) |pz| {
            var ov = false;
            if (readConfigIntoBuf(pz.ptr, cfg_buf[0..], &ov)) |n| {
                out.* = .{ .overflow = ov };
                switch (scanIniPythonFiles(cfg_buf[0..n], "tool:pytest", out)) {
                    .found_key => return,
                    .no_key => {
                        out.* = defaultPyFilePatterns();
                        out.overflow = ov;
                        return;
                    },
                    .no_section => {
                        out.* = defaultPyFilePatterns();
                        out.overflow = ov;
                        return;
                    },
                }
            }
        } else |_| {}

        if (dir_len <= 1) break;
        if (std.mem.lastIndexOfScalar(u8, dir_buf[0..dir_len], '/')) |idx| {
            dir_len = if (idx == 0) 1 else idx;
        } else break;
    }
    out.* = defaultPyFilePatterns();
}
pub fn isIgnoredDir(name: []const u8) bool {
    if (name.len == 0 or name[0] == '.') return true;
    const ignored = [_][]const u8{
        "venv",
        ".venv",
        "node_modules",
        "__pycache__",
        "build",
        "dist",
        "zig-cache",
        "zig-out",
    };
    for (ignored) |ig| {
        if (std.mem.eql(u8, name, ig)) return true;
    }
    return false;
}
