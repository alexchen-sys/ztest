//! Darwin implementation of the small std.os.linux-shaped vocabulary used by
//! ztest. This is an internal PAL interface, not a general Linux emulation:
//! every function maps directly to libSystem and every pidfd is a kqueue watch.
//! Linux targets use std.os.linux directly through pal.posix.

const std = @import("std");
const builtin = @import("builtin");
comptime {
    if (builtin.os.tag != .macos) @compileError("posix_darwin.zig requires macOS");
}

pub const fd_t = i32;
pub const timespec = extern struct { sec: isize, nsec: isize };
pub const AT = struct {
    pub const FDCWD: i32 = -2;
};
pub const F = struct {
    pub const GETFD: i32 = 1;
    pub const SETFD: i32 = 2;
    pub const GETFL: i32 = 3;
    pub const SETFL: i32 = 4;
};
pub const X_OK: i32 = 1;
pub const AF = struct {
    pub const UNIX: i32 = 1;
};
pub const SOCK = struct {
    pub const STREAM: u32 = 1;
    // These are PAL flag bits, stripped before the Darwin socket(2) call.
    pub const NONBLOCK: u32 = 0x10000000;
    pub const CLOEXEC: u32 = 0x20000000;
};
pub const POLL = struct {
    pub const IN: i16 = 0x0001;
    pub const OUT: i16 = 0x0004;
    pub const ERR: i16 = 0x0008;
    pub const HUP: i16 = 0x0010;
    pub const NVAL: i16 = 0x0020;
};
pub const pollfd = extern struct { fd: fd_t, events: i16, revents: i16 };
pub const W = struct {
    pub const NOHANG: i32 = 0x0001;
    pub const EXITED: i32 = 0x0004;
    pub const NOWAIT: i32 = 0x0020;
    pub fn IFEXITED(status: u32) bool {
        return (status & 0x7f) == 0;
    }
    pub fn IFSIGNALED(status: u32) bool {
        return ((status & 0x7f) + 1) >= 2;
    }
    pub fn EXITSTATUS(status: u32) u8 {
        return @intCast((status >> 8) & 0xff);
    }
    pub fn TERMSIG(status: u32) SIG {
        // std.os.linux.W.TERMSIG also returns the SIG enum; main.zig extracts
        // the backing integer, so the Darwin twin must agree on the shape.
        return @fromBackingInt(@intCast(status & 0x7f));
    }
};

/// Darwin fcntl.h values are represented in their native bit positions, so
/// bitCast from fcntl(F_GETFL) in the existing output tests remains meaningful.
pub const O = packed struct(u32) {
    ACCMODE: enum(u2) { RDONLY = 0, WRONLY = 1, RDWR = 2 } = .RDONLY,
    NONBLOCK: bool = false,
    APPEND: bool = false,
    SHLOCK: bool = false,
    EXLOCK: bool = false,
    ASYNC: bool = false,
    SYNC: bool = false,
    NOFOLLOW: bool = false,
    CREAT: bool = false,
    TRUNC: bool = false,
    EXCL: bool = false,
    _12: u5 = 0,
    NOCTTY: bool = false,
    _18: u2 = 0,
    DIRECTORY: bool = false,
    SYMLINK: bool = false,
    DSYNC: bool = false,
    _23: u1 = 0,
    CLOEXEC: bool = false,
    _25: u7 = 0,
};

pub const E = enum(u16) {
    SUCCESS = 0,
    PERM = 1,
    NOENT = 2,
    SRCH = 3,
    INTR = 4,
    IO = 5,
    NXIO = 6,
    NOEXEC = 8,
    BADF = 9,
    CHILD = 10,
    NOMEM = 12,
    ACCES = 13,
    FAULT = 14,
    BUSY = 16,
    EXIST = 17,
    NOTDIR = 20,
    ISDIR = 21,
    INVAL = 22,
    MFILE = 24,
    NOTTY = 25,
    NOSPC = 28,
    PIPE = 32,
    AGAIN = 35,
    MSGSIZE = 40,
    ADDRINUSE = 48,
    CONNRESET = 54,
    NOBUFS = 55,
    TIMEDOUT = 60,
    CONNREFUSED = 61,
    _,
};

pub fn errno(rc: usize) E {
    const signed: isize = @bitCast(rc);
    if (signed < 0 and signed >= -4095) return @fromBackingInt(@intCast(@as(u16, @intCast(-signed))));
    return .SUCCESS;
}

pub const SIG = enum(i32) {
    HUP = 1,
    INT = 2,
    KILL = 9,
    PIPE = 13,
    TERM = 15,
    _,
    // SIG_IGN is the misaligned sentinel address 1 (sigaction(2) ABI): the
    // handler pointer type must be align(1) or @ptrFromInt(1) is rejected on
    // targets whose function pointers carry alignment (aarch64). Same pattern
    // as std.c's SIG.IGN/DFL/ERR sentinels.
    pub const IGN: ?*align(1) const fn (SIG) callconv(.c) void = @ptrFromInt(1);
};
pub const Sigaction = extern struct {
    handler: extern union {
        handler: ?*align(1) const fn (SIG) callconv(.c) void,
        sigaction: ?*const anyopaque,
    },
    mask: u32,
    flags: i32,
};
pub fn sigemptyset() u32 {
    return 0;
}

pub const sockaddr = struct {
    pub const un = extern struct {
        len: u8 = 0,
        family: u8,
        path: [104]u8,
    };
};

const c = struct {
    extern "c" fn __error() *i32;
    extern "c" fn open(path: [*:0]const u8, flags: i32, ...) i32;
    extern "c" fn close(fd: i32) i32;
    extern "c" fn read(fd: i32, buf: ?*anyopaque, n: usize) isize;
    extern "c" fn write(fd: i32, buf: ?*const anyopaque, n: usize) isize;
    extern "c" fn getcwd(buf: [*]u8, size: usize) ?[*]u8;
    extern "c" fn strlen(s: [*]const u8) usize;
    extern "c" fn mkdir(path: [*:0]const u8, mode: u16) i32;
    extern "c" fn mkfifo(path: [*:0]const u8, mode: u16) i32;
    extern "c" fn rmdir(path: [*:0]const u8) i32;
    extern "c" fn unlink(path: [*:0]const u8) i32;
    extern "c" fn symlink(target: [*:0]const u8, linkpath: [*:0]const u8) i32;
    extern "c" fn fchmod(fd: i32, mode: u16) i32;
    extern "c" fn access(path: [*:0]const u8, mode: i32) i32;
    extern "c" fn fcntl(fd: i32, cmd: i32, ...) i32;
    extern "c" fn pipe(fds: *[2]i32) i32;
    extern "c" fn dup2(oldfd: i32, newfd: i32) i32;
    extern "c" fn fork() i32;
    extern "c" fn execve(path: [*:0]const u8, argv: [*:null]const ?[*:0]const u8, envp: [*:null]const ?[*:0]const u8) i32;
    extern "c" fn getpid() i32;
    extern "c" fn getppid() i32;
    extern "c" fn getuid() u32;
    extern "c" fn getpgid(pid: i32) i32;
    extern "c" fn setpgid(pid: i32, pgid: i32) i32;
    extern "c" fn setsid() i32;
    extern "c" fn kill(pid: i32, sig: i32) i32;
    extern "c" fn wait4(pid: i32, status: ?*i32, options: i32, rusage: ?*anyopaque) i32;
    extern "c" fn waitid(idtype: i32, id: u32, info: ?*anyopaque, options: i32) i32;
    extern "c" fn nanosleep(req: *const timespec, rem: ?*timespec) i32;
    extern "c" fn _exit(status: i32) noreturn;
    extern "c" fn socket(domain: i32, type: i32, protocol: i32) i32;
    extern "c" fn socketpair(domain: i32, type: i32, protocol: i32, fds: *[2]i32) i32;
    extern "c" fn bind(fd: i32, addr: ?*anyopaque, len: u32) i32;
    extern "c" fn connect(fd: i32, addr: ?*const anyopaque, len: u32) i32;
    extern "c" fn listen(fd: i32, backlog: i32) i32;
    extern "c" fn accept(fd: i32, addr: ?*anyopaque, len: ?*u32) i32;
    extern "c" fn sendto(fd: i32, buf: ?*const anyopaque, len: usize, flags: i32, addr: ?*const anyopaque, addrlen: u32) isize;
    extern "c" fn recvfrom(fd: i32, buf: ?*anyopaque, len: usize, flags: i32, addr: ?*anyopaque, addrlen: ?*u32) isize;
    extern "c" fn sigaction(sig: i32, act: ?*const Sigaction, old: ?*Sigaction) i32;
    extern "c" fn arc4random_buf(buf: ?*anyopaque, n: usize) void;
    extern "c" fn realpath(path: [*:0]const u8, resolved: [*]u8) ?[*]u8;
    extern "c" fn kqueue() i32;
    extern "c" fn kevent(kq: i32, changes: ?[*]const Kevent, nchanges: i32, events: ?[*]Kevent, nevents: i32, timeout: ?*const timespec) i32;
    extern "c" fn dup(fd: i32) i32;
    extern "c" fn fdopendir(fd: i32) ?*anyopaque;
    extern "c" fn @"fdopendir$INODE64"(fd: i32) ?*anyopaque;
    extern "c" fn closedir(dir: *anyopaque) i32;
    extern "c" fn readdir(dir: *anyopaque) ?*DarwinDirent;
    extern "c" fn @"readdir$INODE64"(dir: *anyopaque) ?*DarwinDirent;
};

fn fail() usize {
    const e = c.__error().*;
    return @bitCast(-@as(isize, e));
}
fn ret(rc: isize) usize {
    if (rc < 0) return fail();
    return @intCast(rc);
}

pub fn open(path: [*:0]const u8, flags: O, mode: u32) usize {
    const native: i32 = @bitCast(@as(u32, @bitCast(flags)));
    return ret(c.open(path, native, mode));
}
pub fn close(fd: fd_t) usize {
    removePidWatch(fd);
    removeDirState(fd);
    return ret(c.close(fd));
}
pub fn read(fd: fd_t, buf: [*]u8, len: usize) usize {
    return ret(c.read(fd, buf, len));
}
pub fn write(fd: fd_t, buf: [*]const u8, len: usize) usize {
    return ret(c.write(fd, buf, len));
}
pub fn getcwd(buf: [*]u8, len: usize) usize {
    if (c.getcwd(buf, len) == null) return fail();
    return c.strlen(buf) + 1;
}
pub fn mkdir(path: [*:0]const u8, mode: u32) usize {
    return ret(c.mkdir(path, @intCast(mode & 0o7777)));
}
pub fn mknod(path: [*:0]const u8, mode: u32, _: u32) usize {
    return ret(c.mkfifo(path, @intCast(mode & 0o7777)));
}
pub fn rmdir(path: [*:0]const u8) usize {
    return ret(c.rmdir(path));
}
pub fn unlink(path: [*:0]const u8) usize {
    return ret(c.unlink(path));
}
pub fn symlink(target: [*:0]const u8, linkpath: [*:0]const u8) usize {
    return ret(c.symlink(target, linkpath));
}
pub fn fchmod(fd: fd_t, mode: u32) usize {
    return ret(c.fchmod(fd, @intCast(mode & 0o7777)));
}
pub fn access(path: [*:0]const u8, mode: i32) usize {
    return ret(c.access(path, mode));
}
pub fn fcntl(fd: fd_t, cmd: i32, arg: usize) usize {
    return ret(c.fcntl(fd, cmd, arg));
}
pub fn dup2(oldfd: fd_t, newfd: fd_t) usize {
    return ret(c.dup2(oldfd, newfd));
}
pub fn getpid() usize {
    return @intCast(c.getpid());
}
pub fn getppid() usize {
    return @intCast(c.getppid());
}
pub fn getuid() u32 {
    // std.os.linux.getuid() also returns uid_t (u32), and main.zig stores the
    // value into a u32 without a cast: the POSIX vocabulary must agree.
    return c.getuid();
}
pub fn setpgid(pid: i32, pgid: i32) usize {
    return ret(c.setpgid(pid, pgid));
}
pub fn setsid() usize {
    return ret(c.setsid());
}
pub fn kill(pid: i32, sig: SIG) usize {
    return ret(c.kill(pid, @backingInt(sig)));
}
pub fn wait4(pid: i32, status: ?*i32, options: i32, rusage: ?*anyopaque) usize {
    return ret(c.wait4(pid, status, options, rusage));
}
pub fn nanosleep(req: *const timespec, rem: ?*timespec) usize {
    return ret(c.nanosleep(req, rem));
}
pub fn exit_group(status: i32) noreturn {
    c._exit(status);
}
pub fn fork() usize {
    return ret(c.fork());
}
pub fn execve(path: [*:0]const u8, argv: [*:null]const ?[*:0]const u8, envp: [*:null]const ?[*:0]const u8) usize {
    return ret(c.execve(path, argv, envp));
}
pub fn getrandom(buf: [*]u8, len: usize, flags: u32) usize {
    _ = flags;
    c.arc4random_buf(buf, len);
    return len;
}
pub fn sigaction(sig: SIG, act: ?*const Sigaction, old: ?*Sigaction) usize {
    return ret(c.sigaction(@backingInt(sig), act, old));
}

pub fn pipe2(fds: *[2]fd_t, flags: O) usize {
    const rc = c.pipe(fds);
    if (rc < 0) return fail();
    if (flags.CLOEXEC) {
        _ = c.fcntl(fds[0], F.SETFD, @as(usize, 1));
        _ = c.fcntl(fds[1], F.SETFD, @as(usize, 1));
    }
    if (flags.NONBLOCK) {
        for (fds) |fd| {
            const fl = c.fcntl(fd, F.GETFL, @as(usize, 0));
            if (fl >= 0) _ = c.fcntl(fd, F.SETFL, @as(usize, @intCast(fl | 0x0004)));
        }
    }
    return 0;
}

fn applySocketFlags(fd: fd_t, flags: u32) void {
    if (flags & SOCK.CLOEXEC != 0) _ = c.fcntl(fd, F.SETFD, @as(usize, 1));
    if (flags & SOCK.NONBLOCK != 0) {
        const fl = c.fcntl(fd, F.GETFL, @as(usize, 0));
        if (fl >= 0) _ = c.fcntl(fd, F.SETFL, @as(usize, @intCast(fl | 0x0004)));
    }
}
pub fn socket(domain: i32, type_flags: u32, protocol: i32) usize {
    const fd = c.socket(domain, @intCast(type_flags & 0x0fffffff), protocol);
    if (fd < 0) return fail();
    applySocketFlags(fd, type_flags);
    return @intCast(fd);
}
pub fn socketpair(domain: i32, type_flags: u32, protocol: i32, fds: *[2]fd_t) usize {
    const rc = c.socketpair(domain, @intCast(type_flags & 0x0fffffff), protocol, fds);
    if (rc < 0) return fail();
    for (fds) |fd| applySocketFlags(fd, type_flags);
    return 0;
}
pub fn bind(fd: fd_t, addr: ?*anyopaque, len: u32) usize {
    if (addr) |p| {
        const un: *sockaddr.un = @ptrCast(@alignCast(p));
        un.len = @intCast(@min(len, @sizeOf(sockaddr.un)));
    }
    return ret(c.bind(fd, addr, len));
}
pub fn connect(fd: fd_t, addr: ?*const anyopaque, len: u32) usize {
    return ret(c.connect(fd, addr, len));
}
pub fn listen(fd: fd_t, backlog: i32) usize {
    return ret(c.listen(fd, backlog));
}
pub fn accept4(fd: fd_t, addr: ?*anyopaque, len: ?*u32, flags: u32) usize {
    const accepted = c.accept(fd, addr, len);
    if (accepted < 0) return fail();
    applySocketFlags(accepted, flags);
    return @intCast(accepted);
}
pub fn sendto(fd: fd_t, buf: [*]const u8, len: usize, flags: u32, addr: ?*const anyopaque, addrlen: u32) usize {
    return ret(c.sendto(fd, buf, len, @intCast(flags), addr, addrlen));
}
pub fn recvfrom(fd: fd_t, buf: [*]u8, len: usize, flags: u32, addr: ?*anyopaque, addrlen: ?*u32) usize {
    return ret(c.recvfrom(fd, buf, len, @intCast(flags), addr, addrlen));
}

/// Canonicalization without Linux O_PATH/readlinkat(AT_EMPTY_PATH): realpath(3)
/// resolves the same symlink chain. `out` must hold PATH_MAX bytes (4096 here).
pub fn canonicalizePath(path: [*:0]const u8, out: [*]u8) ?usize {
    const resolved = c.realpath(path, out) orelse return null;
    return c.strlen(resolved);
}

// Raw-syscall compatibility is intentionally tiny and exists for the old
// containment tests. Production paths use typed PAL calls instead.
pub const SYS = enum { getpgid, prctl, kill, waitid };
pub fn syscall1(number: SYS, arg1: usize) usize {
    return switch (number) {
        .getpgid => ret(c.getpgid(@intCast(arg1))),
        .kill => ret(c.kill(@intCast(arg1), 0)),
        else => failWith(.INVAL),
    };
}
pub fn syscall2(number: SYS, arg1: usize, arg2: usize) usize {
    return switch (number) {
        .kill => ret(c.kill(@intCast(arg1), @intCast(arg2))),
        // Darwin has no subreaper; ADR-004 uses libproc/group containment.
        .prctl => if (arg1 == 36) 0 else failWith(.INVAL),
        .getpgid => ret(c.getpgid(@intCast(arg1))),
        else => failWith(.INVAL),
    };
}
pub fn syscall5(number: SYS, arg1: usize, arg2: usize, arg3: usize, arg4: usize, arg5: usize) usize {
    _ = arg5;
    if (number != .waitid) return failWith(.INVAL);
    var info: [128]u8 align(8) = @splat(0);
    const rc = c.waitid(@intCast(arg1), @intCast(arg2), &info, @intCast(arg4));
    if (rc < 0) return fail();
    const out: *[128]u8 align(8) = @ptrFromInt(arg3);
    // Internal PAL siginfo layout: typed readers use siginfoPid(), never a
    // Darwin ABI assumption. The two pid words are at 16 and 20 explicitly.
    out.* = info;
    return 0;
}
fn failWith(e: E) usize {
    return @bitCast(-@as(isize, @backingInt(e)));
}
pub fn siginfoPid(buf: *const [128]u8) i32 {
    return std.mem.readInt(i32, buf[16..20], .little);
}

// ---------------------------------------------------------------------------
// kqueue process handles and poll adaptation
// ---------------------------------------------------------------------------
pub const Kevent = extern struct {
    ident: usize,
    filter: i16,
    flags: u16,
    fflags: u32,
    data: isize,
    udata: ?*anyopaque,
};
pub const EVFILT_READ: i16 = -1;
pub const EVFILT_WRITE: i16 = -2;
pub const EVFILT_PROC: i16 = -5;
pub const EV_ADD: u16 = 0x0001;
pub const EV_ENABLE: u16 = 0x0004;
pub const EV_ONESHOT: u16 = 0x0010;
pub const EV_CLEAR: u16 = 0x0020;
pub const EV_EOF: u16 = 0x8000;
pub const EV_ERROR: u16 = 0x4000;
pub const NOTE_EXIT: u32 = 0x80000000;
pub const NOTE_FORK: u32 = 0x40000000;
pub const NOTE_TRACK: u32 = 0x00000002;

const PidWatch = struct { fd: i32 = -1, pid: i32 = 0, exited: bool = false };
var pid_watches: [64]PidWatch = @splat(.{});
fn removePidWatch(fd: i32) void {
    for (&pid_watches) |*w| {
        if (w.fd == fd) w.* = .{};
    }
}
fn watchedSlot(fd: i32) ?*PidWatch {
    for (&pid_watches) |*w| {
        if (w.fd == fd and w.fd >= 0) return w;
    }
    return null;
}

pub fn pidfdOpen(pid: i32) fd_t {
    if (pid <= 0) return -1;
    var free_slot: ?*PidWatch = null;
    for (&pid_watches) |*w| {
        if (w.fd == -1) {
            free_slot = w;
            break;
        }
    }
    const slot = free_slot orelse return -1;
    const kq = c.kqueue();
    if (kq < 0) return -1;
    var change = Kevent{
        .ident = @intCast(pid),
        .filter = EVFILT_PROC,
        .flags = EV_ADD | EV_ENABLE | EV_ONESHOT,
        .fflags = NOTE_EXIT,
        .data = 0,
        .udata = null,
    };
    if (c.kevent(kq, @ptrCast(&change), 1, null, 0, null) < 0) {
        // On XNU, EVFILT_PROC registration on a child that has already transitioned
        // to SZOMB returns ESRCH, whereas kill(pid, 0) still succeeds (0) on a zombie
        // until it is reaped (and fails with ESRCH on nonexistent/invalid PIDs).
        if (c.kill(pid, 0) == 0) {
            slot.* = .{ .fd = kq, .pid = pid, .exited = true };
            return kq;
        }
        _ = c.close(kq);
        return -1;
    }
    slot.* = .{ .fd = kq, .pid = pid, .exited = false };
    return kq;
}

/// Blocking wait for one process handle (used by the run watchdog).
pub fn waitPidfdExit(fd: fd_t) bool {
    if (watchedSlot(fd)) |w| {
        if (w.exited) return true;
    }
    var ev: [1]Kevent = undefined;
    while (true) {
        const n = c.kevent(fd, null, 0, &ev, 1, null);
        if (n < 0) return false;
        if (n > 0) {
            if (watchedSlot(fd)) |w| w.exited = true;
            return true;
        }
    }
}

pub fn poll(fds: [*]pollfd, nfds: usize, timeout_ms: i32) usize {
    if (nfds > 64) return failWith(.INVAL);
    for (fds[0..nfds]) |*p| p.revents = 0;
    if (nfds == 0) {
        if (timeout_ms <= 0) return 0;
        const req = timespec{ .sec = @divTrunc(timeout_ms, 1000), .nsec = @rem(timeout_ms, 1000) * 1_000_000 };
        return nanosleep(&req, null);
    }
    const kq = c.kqueue();
    if (kq < 0) return fail();
    defer _ = c.close(kq);
    var changes: [64]Kevent = undefined;
    var events: [64]Kevent = undefined;
    var count: usize = 0;
    var ready: usize = 0;
    for (fds[0..nfds], 0..) |*p, idx| {
        if (watchedSlot(p.fd)) |w| {
            if (w.exited) {
                if (p.events & POLL.IN != 0) {
                    p.revents = POLL.IN;
                    ready += 1;
                }
                continue;
            }
            if (p.events & POLL.IN != 0) {
                changes[count] = .{
                    .ident = @intCast(p.fd),
                    .filter = EVFILT_READ,
                    .flags = EV_ADD | EV_ENABLE | EV_CLEAR,
                    .fflags = 0,
                    .data = 0,
                    .udata = @ptrFromInt(idx + 1),
                };
                count += 1;
            }
            continue;
        }
        const filters = [_]i16{ EVFILT_READ, EVFILT_WRITE };
        for (filters) |filter| {
            if (filter == EVFILT_READ and p.events & POLL.IN == 0) continue;
            if (filter == EVFILT_WRITE and p.events & POLL.OUT == 0) continue;
            changes[count] = .{
                .ident = @intCast(p.fd),
                .filter = filter,
                .flags = EV_ADD | EV_ENABLE | EV_CLEAR,
                .fflags = 0,
                .data = 0,
                .udata = @ptrFromInt(idx + 1),
            };
            count += 1;
        }
    }
    if (count == 0) return ready;
    var ts: timespec = undefined;
    var timeout_ptr: ?*const timespec = null;
    if (ready > 0) {
        ts = .{ .sec = 0, .nsec = 0 };
        timeout_ptr = &ts;
    } else if (timeout_ms >= 0) {
        ts = .{ .sec = @divTrunc(timeout_ms, 1000), .nsec = @rem(timeout_ms, 1000) * 1_000_000 };
        timeout_ptr = &ts;
    }
    const n = c.kevent(kq, &changes, @intCast(count), &events, @intCast(events.len), timeout_ptr);
    if (n < 0) return if (ready > 0) ready else fail();
    for (events[0..@intCast(n)]) |ev| {
        const idx = @intFromPtr(ev.udata orelse continue);
        if (idx == 0 or idx > nfds) continue;
        const p = &fds[idx - 1];
        var bits: i16 = 0;
        if (ev.filter == EVFILT_PROC or ev.filter == EVFILT_READ) bits |= POLL.IN;
        if (ev.filter == EVFILT_WRITE) bits |= POLL.OUT;
        if (ev.flags & EV_EOF != 0) bits |= POLL.HUP;
        if (ev.flags & EV_ERROR != 0) bits |= POLL.ERR;
        if (p.revents == 0) ready += 1;
        p.revents |= bits;
    }
    return ready;
}

// ---------------------------------------------------------------------------
// getdents64-compatible discovery records (LinuxDirent64 is the neutral format)
// ---------------------------------------------------------------------------
const DarwinDirent = extern struct {
    ino: u64,
    seekoff: u64,
    reclen: u16,
    namlen: u16,
    type: u8,
    name: [1024]u8,
};
/// The neutral discovery record shared by every backend: the exact linux
/// dirent64 wire layout (ino@0, off@8, reclen@16, type@18, name@19). main.zig
/// parses this format on every OS; getdents64 below converts Darwin's native
/// readdir records into it. Single source of the layout for the Darwin side
/// (re-exported as pal.LinuxDirent64 via darwin.zig) so the writer and the
/// shared parser can never drift apart.
pub const LinuxDirent64 = extern struct {
    d_ino: u64,
    d_off: i64,
    d_reclen: u16,
    d_type: u8,
    d_name: [1]u8,
};
const DirState = struct {
    fd: i32 = -1,
    dir: ?*anyopaque = null,
    pending: ?DarwinDirent = null,
    eof: bool = false,
};
var dir_states: [16]DirState = @splat(.{});
fn removeDirState(fd: i32) void {
    for (&dir_states) |*s| {
        if (s.fd == fd) {
            if (s.dir) |dir| {
                _ = c.closedir(dir);
            }
            s.* = .{};
        }
    }
}
fn dirState(fd: i32) ?*DirState {
    var free_slot: ?*DirState = null;
    for (&dir_states) |*s| {
        if (s.fd == fd) return s;
        if (s.fd == -1 and free_slot == null) free_slot = s;
    }
    if (free_slot) |s| {
        const dup_fd = c.dup(fd);
        if (dup_fd < 0) return null;
        const open_dir = if (builtin.cpu.arch == .x86_64) c.@"fdopendir$INODE64"(dup_fd) else c.fdopendir(dup_fd);
        const dir = open_dir orelse {
            _ = c.close(dup_fd);
            return null;
        };
        s.* = .{ .fd = fd, .dir = dir };
        return s;
    }
    return null;
}

fn readDir(dir: *anyopaque) ?*DarwinDirent {
    // Darwin's 64-bit-inode ABI uses a versioned symbol on x86_64 only.
    if (builtin.cpu.arch == .x86_64) return c.@"readdir$INODE64"(dir);
    return c.readdir(dir);
}

/// Reads native Darwin directory records and emits the packed records consumed
/// by the shared discovery loop. Conversion is bounded and allocation-free;
/// per-fd state preserves a record that does not fit the caller's buffer.
pub fn getdents64(fd: fd_t, out: [*]u8, out_len: u32) usize {
    const state = dirState(fd) orelse return failWith(.NOMEM);
    const dir = state.dir orelse return failWith(.BADF);
    var written: usize = 0;
    while (true) {
        if (state.eof) return written;
        const d: *const DarwinDirent = if (state.pending) |*pending| pending else blk: {
            c.__error().* = 0;
            const next = readDir(dir) orelse {
                if (c.__error().* != 0) return fail();
                state.eof = true;
                return written;
            };
            break :blk next;
        };
        const name_len: usize = @min(d.namlen, 1024);
        const record_len = std.mem.alignForward(usize, 19 + name_len + 1, 8);
        if (written + record_len > out_len) {
            if (state.pending == null) state.pending = d.*;
            if (written == 0) return failWith(.INVAL);
            return written;
        }
        const dst: *LinuxDirent64 = @ptrCast(@alignCast(out + written));
        dst.* = .{ .d_ino = d.ino, .d_off = @intCast(d.seekoff), .d_reclen = @intCast(record_len), .d_type = d.type, .d_name = .{0} };
        @memset(out[written + 19 .. written + record_len], 0);
        @memcpy(out[written + 19 ..][0..name_len], d.name[0..name_len]);
        written += record_len;
        state.pending = null;
    }
}
