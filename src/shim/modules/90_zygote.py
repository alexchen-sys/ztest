# ztest shim source module: 90_zygote
# Part of the generated bundle src/shim/ztest_shim.py (ADR-006 v2).
# Edit this module, then regenerate with: python3 scripts/bundle_shim.py
# Provides/requires are documented by the section banner of the original
# monolith kept verbatim below; no cross-module import machinery exists.
# ============================================================================
# 9. Zygote Server & Clean-Spawn Entrypoints
# ============================================================================

def _connect_ipc_fd199(ipc_sock_path: str) -> int:
    global _ipc_active_fd
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
    except OSError:
        pass
    s.connect(ipc_sock_path)
    raw_fd = s.fileno()
    if raw_fd != IPC_TARGET_FD:
        os.dup2(raw_fd, IPC_TARGET_FD)
        s.close()
    else:
        s.detach()
    os.set_inheritable(IPC_TARGET_FD, False)
    # fd 199 is non-blocking — send_event works via poll(POLLOUT) + deadline.
    os.set_blocking(IPC_TARGET_FD, False)
    try:
        import fcntl
        _fd_flags = fcntl.fcntl(IPC_TARGET_FD, fcntl.F_GETFD)
        fcntl.fcntl(IPC_TARGET_FD, fcntl.F_SETFD, _fd_flags | fcntl.FD_CLOEXEC)
    except (ImportError, OSError):
        pass
    _ipc_active_fd = IPC_TARGET_FD
    return IPC_TARGET_FD


def _zygote_thread_count() -> int:
    try:
        return len(os.listdir("/proc/self/task"))
    except Exception:
        pass
    # Darwin has no /proc; use threading.active_count() as a portable fallback.
    # The zygote is single-threaded at startup, so 1 is the honest answer.
    try:
        import threading
        return threading.active_count()
    except Exception:
        return 2  # fail-closed: treat unreadable state as multi-threaded


_DARWIN_LIBC = ctypes.CDLL(None, use_errno=True) if sys.platform == "darwin" else None


def _waitid_peek_exited(pid: int) -> object:
    if sys.platform != "darwin" and hasattr(os, "waitid"):
        return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    libc = _DARWIN_LIBC or ctypes.CDLL(None, use_errno=True)
    si = (ctypes.c_int32 * 32)()
    rc = libc.waitid(
        ctypes.c_int32(1),
        ctypes.c_uint32(pid),
        ctypes.byref(si),
        ctypes.c_int32(0x01 | 0x04 | 0x20),
    )
    if rc != 0:
        err = ctypes.get_errno()
        if err == errno.ECHILD:
            raise ChildProcessError(err, "No child processes")
        return None
    return True if si[3] == pid else None


def run_zygote_server(ctrl_sock_path: str, ipc_sock_path: str, supervisor_pid: int) -> None:
    # setpgid runs before connect — the accept-time invariant getpgid(zygote)==zygote is checked via SO_PEERCRED; a mismatch rejects the zygote. Invariants: ZYGOTE-GROUP-IDENTITY.
    try:
        os.setpgid(0, 0)
    except OSError:
        pass
    # Re-arming the zygote's pdeathsig — covers the exec→python-startup window; prctl is idempotent. Invariants: ADR-004-1.
    _arm_pdeathsig(supervisor_pid, watch_thread=False)
    # Prevent native BLAS/OpenMP libraries imported by root conftest.py from spawning
    # background thread pools before fork(), keeping the zygote single-threaded.
    for _thr_env in ("OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "OMP_NUM_THREADS", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
        os.environ.setdefault(_thr_env, "1")
    # 1. Pre-import clean whitelist + optional packages + root conftest.py if single-threaded
    _is_pytest_engine = os.environ.get("ZTEST_ENGINE") == "pytest"
    if _is_pytest_engine:
        try:
            import pluggy  # noqa: F401
            import _pytest  # noqa: F401
            import pytest  # noqa: F401
        except Exception:
            pass
    else:
        install_pytest_modules()
    cwd_path = Path(os.getcwd()).resolve()
    if str(cwd_path) not in sys.path:
        sys.path.insert(0, str(cwd_path))
    # On small suites (files <= 4) the supervisor sets ZTEST_PRELOAD_HEAVY=0
    # so the zygote skips optional pre-imports that would not amortize.
    # Third-party preloading is opt-in via --preload / ZTEST_PRELOAD_PKGS
    # (comma-separated dotted module names); by default only the standard
    # library modules below are pre-imported before gc.freeze().
    if os.environ.get("ZTEST_PRELOAD_HEAVY") != "0":
        for _opt_pkg in os.environ.get("ZTEST_PRELOAD_PKGS", "").split(","):
            _opt_pkg = _opt_pkg.strip()
            if not _opt_pkg or not all(_part.isidentifier() for _part in _opt_pkg.split(".")):
                continue
            try:
                __import__(_opt_pkg)
            except Exception:
                pass

        # v0.6.0: best-effort import of the project package itself. An explicit
        # ZTEST_PROJECT_PKG always wins; --no-auto-preload / ZTEST_AUTO_PRELOAD=0
        # disables this whole block (the explicit --preload list above still applies).
        # The detector is a separate stdlib-only module, extracted next to this shim.
        if os.environ.get("ZTEST_AUTO_PRELOAD", "1") != "0":
            try:
                import _project_detect as _pd
                _pkg = os.environ.get("ZTEST_PROJECT_PKG") or _pd.detect_project_package()
                if _pkg:
                    _pd.import_project_package(_pkg)
            except Exception:
                pass  # never abort a run because of optional project preloading

    # Pre-import the shim's lazy stdlib modules, otherwise every worker pays their
    # import anew (configparser ~5-8 ms/file, tomllib 20-30 ms, sysconfig,
    # fractions; importlib.metadata — up to 50-70 ms per file for entry_points
    # lexer/codec scans). Idempotent stdlib, no network/file side effects: done
    # in the zygote before gc.freeze, the workers get it via COW. An import error
    # does not kill the zygote (the per-file path imports it itself). Gated by the
    # same files<=8 threshold as the optional package preload above.
    if os.environ.get("ZTEST_PRELOAD_HEAVY") != "0":
        for _std_mod in ("configparser", "tomllib", "sysconfig", "fractions", "importlib.metadata"):
            try:
                __import__(_std_mod)
            except Exception:
                pass

    # Preload root conftests into the zygote; only KeyboardInterrupt propagates, each preload uses a unique module name, and failed loads are evicted from sys.modules. Invariants: ZYGOTE-PRELOAD.
    if not _is_pytest_engine:
        for _cf_idx, rel_cf in enumerate(("conftest.py", "tests/conftest.py")):
            cf_candidate = cwd_path / rel_cf
            if not cf_candidate.exists():
                continue
            try:
                abs_cf = str(cf_candidate.resolve())
                cf_mod = load_module_with_ast_cache(cf_candidate, _package_qualified_name(cf_candidate, f"_ztest_conftest_preload_{_cf_idx}"), cache_dir=cwd_path / ".ztest_cache")
            except KeyboardInterrupt:
                raise
            except BaseException:
                continue  # the zygote survives; the worker reports the real conftest error per-file
            if _zygote_thread_count() == 1:
                _PRELOADED_CONFTESTS[abs_cf] = cf_mod
    faulthandler.register(signal.SIGQUIT, file=sys.stderr, all_threads=True)
    # ZYGOTE-SWEEP-EXCLUDED: the zygote's pdeathsig is armed at the TOP of run_zygote_server
    # (before install_pytest_modules and the conftest preload) — see the
    # comment there.

    # Prewarm the ini cache before gc.freeze() — the workers get it via COW and
    # pay nothing per file for ini options (~16 ms: the configparser import +
    # re.compile). A prewarm error is silenced: the per-file path reads the ini
    # itself. (Conftest fixture introspection is per-file against the live
    # module — there is no introspection prewarm.)
    try:
        _warm_ini_options_cache(cwd_path)
    except BaseException:
        pass
    # The pytest11 entry-point list is a sys.path-wide scan; computed once in
    # the zygote, the workers inherit it via COW instead of paying it per file.
    _warm_pytest11_entrypoint_cache()

    # Return the glibc free chunks to the OS before freeze — the workers get a
    # trimmed address space via COW (see _trim_free_arenas).
    _trim_free_arenas()
    # The zygote's pdeathsig is armed at the TOP of run_zygote_server
    # (before install_pytest_modules and the conftest preload) — see the
    # comment there.

    gc.collect()
    gc.freeze()

    zygote_pid = os.getpid()
    ctrl_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    ctrl_sock.connect(ctrl_sock_path)
    ctrl_file = ctrl_sock.makefile("rw", encoding="utf-8", buffering=1)
    if _zygote_thread_count() > 1:
        # Never os.fork() from a multi-threaded zygote: an inherited mutex locked
        # by a background thread at fork time can deadlock the child worker.
        # Signal the supervisor to fall back cleanly to per-file exec mode.
        try:
            ctrl_file.write(f"MULTITHREADED\t{zygote_pid}\n")
            ctrl_file.flush()
            ctrl_file.close()
            ctrl_sock.close()
        except Exception:
            pass
        return
    ctrl_file.write(f"READY\t{zygote_pid}\n")
    ctrl_file.flush()

    active_children: set[int] = set()
    exited_children: set[int] = set()  # pinned zombies: WNOWAIT-detected, not yet reaped
    draining = False

    def _poll_children() -> None:
        # A WNOWAIT mark + a group sweep without waitpid — the zombie pins pid/pgid from reuse, the status is taken by the REAP handshake. Invariants: ZOMBIE-PINNING.
        for cpid in list(active_children):
            if cpid in exited_children:
                continue
            try:
                info = _waitid_peek_exited(cpid)
                if info is not None:
                    try:
                        os.kill(-cpid, signal.SIGKILL)
                    except OSError:
                        pass
                    exited_children.add(cpid)
            except ChildProcessError:
                active_children.discard(cpid)
                exited_children.discard(cpid)

    def _poll_children_drain() -> None:
        # During DRAIN the supervisor is gone: fully reap exited children so the loop can terminate.
        for cpid in list(active_children):
            try:
                info = _waitid_peek_exited(cpid)
                if info is not None:
                    try:
                        os.kill(-cpid, signal.SIGKILL)
                    except OSError:
                        pass
                    try:
                        os.waitpid(cpid, 0)
                    except ChildProcessError:
                        pass
                    active_children.discard(cpid)
                    exited_children.discard(cpid)
            except ChildProcessError:
                active_children.discard(cpid)
                exited_children.discard(cpid)

    while True:
        if draining:
            _poll_children_drain()
        else:
            _poll_children()
        if draining and not active_children:
            break

        # Non-blocking wait when draining so WNOHANG can reap exiting workers
        if draining:
            rlist, _, _ = select.select([ctrl_sock], [], [], 0.02)
            if not rlist:
                continue

        line = ctrl_file.readline()
        if not line:
            # EOF on the ctrl socket = the supervisor's death → killpg of all live workers (including non-accepted) → _exit; SIGCHLD=SIG_IGN is forbidden. Invariants: ADR-004-2.
            for cpid in list(active_children):
                try:
                    os.killpg(cpid, signal.SIGKILL)
                except OSError:
                    pass
            try:
                _real_stderr.write("ztest-shim: supervisor EOF; killed all worker groups\n")
                _real_stderr.flush()
            except Exception:
                pass
            os._exit(0)

        line = line.rstrip("\r\n")
        if line == "DRAIN":
            draining = True
            try:
                ctrl_file.write("DRAINING\n")
                ctrl_file.flush()
            except BrokenPipeError:
                break
            continue

        if line.startswith("REAP\t"):
            target_pid = int(line.split("\t")[1])
            _poll_children()
            if target_pid not in active_children:
                active_children.discard(target_pid)
                exited_children.discard(target_pid)
                # Never return 0 on an unknown/lost child — the REAP answer is -1
                ctrl_file.write(f"REAPED\t{target_pid}\t-1\n")
                ctrl_file.flush()
                continue
            # Bounded REAP 2.5s → SIGKILL → a 1s final reap; the answer is -1. Invariants: ZYGOTE-REAP-BOUNDED.
            status: int = -1
            reap_until = _REAL_MONOTONIC() + 2.5
            _reap_spins = 0
            while True:
                try:
                    info = _waitid_peek_exited(target_pid)
                except ChildProcessError:
                    info = None
                    status = -1
                    break
                if info is not None:
                    try:
                        os.kill(-target_pid, signal.SIGKILL)
                    except OSError:
                        pass
                    try:
                        _wpid, status = os.waitpid(target_pid, 0)
                    except ChildProcessError:
                        status = -1
                    break
                if _REAL_MONOTONIC() >= reap_until:
                    try:
                        os.kill(-target_pid, signal.SIGKILL)
                        os.kill(target_pid, signal.SIGKILL)
                    except OSError:
                        pass
                    final_until = _REAL_MONOTONIC() + 1.0
                    while _REAL_MONOTONIC() < final_until:
                        try:
                            info = _waitid_peek_exited(target_pid)
                        except ChildProcessError:
                            info = None
                            break
                        if info is not None:
                            try:
                                _wpid, status = os.waitpid(target_pid, 0)
                            except ChildProcessError:
                                status = -1
                            break
                        time.sleep(0.005)
                    break
                _reap_spins += 1
                time.sleep(0.0002 if _reap_spins <= 10 else 0.002)
            active_children.discard(target_pid)
            exited_children.discard(target_pid)
            ctrl_file.write(f"REAPED\t{target_pid}\t{status}\n")
            ctrl_file.flush()
            continue

        if line.startswith("FORK\t"):
            # The JSON FORK protocol; a parse error → an ERR line, the zygote keeps serving (fallback: kill zygote → exec retry). Invariants: FORK-JSON-FRAMING.
            try:
                fork_req = json.loads(line[len("FORK\t"):])
                if not isinstance(fork_req, dict):
                    raise ValueError("FORK request is not a JSON object")
                slot_ipc_sock = fork_req["slot"]
                filepath_str = fork_req["file"]
                options = fork_req["opts"]
                if not isinstance(slot_ipc_sock, str) or not isinstance(filepath_str, str) or not isinstance(options, dict):
                    raise ValueError("FORK request fields have wrong types")
            except Exception as fork_err:
                try:
                    ctrl_file.write("ERR\t" + json.dumps(f"{type(fork_err).__name__}: {fork_err}") + "\n")
                    ctrl_file.flush()
                except Exception:
                    pass
                continue
            if _zygote_thread_count() > 1:
                try:
                    ctrl_file.write("ERR\t\"zygote became multi-threaded; refusing os.fork()\"\n")
                    ctrl_file.flush()
                except Exception:
                    pass
                break
            # A pipe handshake confirming setpgid — FORKED is sent only after the
            # child has certainly entered its own group (otherwise the accept-time
            # checkPeerPgid could catch the parent's pgid → a false CRASHED). If the
            # child died before the handshake — read returns b'' on pipe close.
            spg_r, spg_w = os.pipe()
            child_pid = os.fork()
            if child_pid > 0:
                os.close(spg_w)
                try:
                    os.setpgid(child_pid, child_pid)
                except OSError:
                    pass
                try:
                    os.read(spg_r, 1)
                except OSError:
                    pass
                os.close(spg_r)
                active_children.add(child_pid)
                ctrl_file.write(f"FORKED\t{child_pid}\n")
                ctrl_file.flush()
            else:
                os.close(spg_r)
                try:
                    os.setpgid(0, 0)
                except OSError:
                    pass
                try:
                    os.write(spg_w, b"1")
                except OSError:
                    pass
                os.close(spg_w)
                # The worker's signal semantics = plain python (pytest parity):
                # SIGPIPE stays SIG_IGN (inherited from the supervisor through
                # execve/fork). A write to a closed pipe/socket — BrokenPipeError,
                # not death by signal 13.
                # Shell pipelines of tests are protected by subprocess
                # restore_signals=True (the CPython default: SIGPIPE/SIGXFZ/SIGXFSZ →
                # SIG_DFL in the child) — no separate reset at the worker level.
                try:
                    ctrl_file.close()
                    ctrl_sock.close()
                except Exception:
                    pass
                # register_at_fork before opening fd 199 — the "fd open, handler not
                # registered" window is fully closed.
                os.register_at_fork(after_in_child=_close_ipc_in_grandchild)
                ipc_fd = _connect_ipc_fd199(slot_ipc_sock)
                _close_extra_fds({0, 1, 2, ipc_fd})
                random.seed(os.urandom(16))
                ipc = IpcChannel(ipc_fd, os.getpid())
                # The worker re-arms pdeathsig onto the zygote post-fork; the group is killed by the kernel (cascade + EOF watch) and the Zig batch watchdog. Invariants: ADR-004-1.
                _arm_pdeathsig(zygote_pid)
                rc = run_test_file(filepath_str, options, ipc)
                try:
                    os.close(ipc_fd)
                except OSError:
                    pass
                os._exit(rc)


def run_clean_spawn_worker(ipc_sock_path: str, filepath_str: str, opts_json: str, supervisor_pid: int) -> None:
    # Clean-spawn worker: pdeathsig is armed pre-execve in the Zig child branch
    # (spawn_arm.zig) and survives exec, so there is no re-arm here. This entry
    # point creates the process group (ARM-ORDER); pdeathsig = SIGKILL —
    # the kernel kills the worker GIL-independently (see INVARIANTS.md).
    try:
        os.setpgid(0, 0)
    except OSError:
        pass
    # Clean-spawn is armed pre-exec (no re-arm needed). Invariants: ADR-004-1.
    # SIGPIPE: python semantics (SIG_IGN), as in the zygote path
    # (both modes must match); the shell pipelines of tests are protected by
    # subprocess restore_signals=True.
    # register_at_fork before any code able to fork (install_pytest_modules may
    # pull multiprocessing imports) and before connect.
    os.register_at_fork(after_in_child=_close_ipc_in_grandchild)
    if os.environ.get("ZTEST_ENGINE") != "pytest":
        install_pytest_modules()
    # clean-spawn forks no workers, but this process's PSS = the file's PSS: the
    # shim's compilation garbage + the AST pre-imports are returned to the OS right
    # after install_pytest_modules() (before the user module import and its AST).
    _trim_free_arenas()
    ipc_fd = _connect_ipc_fd199(ipc_sock_path)
    _close_extra_fds({0, 1, 2, ipc_fd})
    options = json.loads(opts_json)
    ipc = IpcChannel(ipc_fd, os.getpid())
    rc = run_test_file(filepath_str, options, ipc)
    try:
        os.close(ipc_fd)
    except OSError:
        pass
    os._exit(rc)


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "--zygote":
        run_zygote_server(sys.argv[2], sys.argv[3], int(sys.argv[4]))
    elif len(sys.argv) >= 6 and sys.argv[1] == "--worker":
        run_clean_spawn_worker(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]))
    else:
        sys.stderr.write("Usage: ztest_shim.py [--zygote <ctrl_sock> <ipc_sock> <sup_pid> | --worker <ipc_sock> <file> <opts_json> <sup_pid>]\n")
        sys.exit(2)
