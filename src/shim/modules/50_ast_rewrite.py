# ztest shim source module: 50_ast_rewrite
# Part of the generated bundle src/shim/ztest_shim.py (ADR-006 v2).
# Edit this module, then regenerate with: python3 scripts/bundle_shim.py
# Provides/requires are documented by the section banner of the original
# monolith kept verbatim below; no cross-module import machinery exists.
# ============================================================================
# 5. AST Assert Rewriter + SHA-256 Cache (.ztest_cache/ast/)
# ============================================================================

def _ztest_assert_cmp(op: str, left: Any, right: Any) -> bool:
    # Bool-predicate rewrite delegating to bool(res). Invariants: REWRITE-PRESERVES-SCOPE, ARRAY-TRUTH-FALLBACK.
    res: Any
    if op == "==":
        res = left == right
    elif op == "!=":
        res = left != right
    elif op == "<":
        res = left < right
    elif op == "<=":
        res = left <= right
    elif op == ">":
        res = left > right
    elif op == ">=":
        res = left >= right
    elif op == "in":
        res = left in right
    elif op == "not in":
        res = left not in right
    elif op == "is":
        res = left is right
    elif op == "is not":
        res = left is not right
    else:
        res = False
    # Propagate ValueError on ambiguous multi-element array truth (pytest parity). Invariants: ALL-FALLBACK-SHAPE-GATED.
    return bool(res)


def _safe_assert_repr(val: Any) -> str:
    try:
        return repr(val)
    except Exception as exc:
        return f"<[{type(exc).__name__}({str(exc)!r}) raised in repr()] {type(val).__name__} object at 0x{id(val):x}>"


def _ztest_structured_diff(op: str, left: Any, right: Any) -> str | None:
    """Human-readable diff for ``assert a == b`` on containers and multiline strings.

    Returns None when the operands are not worth a structural diff (small
    scalars), so the caller falls back to the plain Left/Right rendering.
    Never raises: user containers may hold objects whose repr or __eq__ is
    broken, and a diagnostic helper must not replace the original AssertionError.
    Output is capped like the unified diff so --llm stays compact.
    """
    import difflib
    import pprint

    def _lines(value: Any) -> list[str]:
        if isinstance(value, str):
            return value.splitlines()
        if isinstance(value, (set, frozenset)):
            try:
                ordered = sorted(value)
            except TypeError:
                ordered = sorted(value, key=repr)
            return [_safe_assert_repr(v) for v in ordered]
        if isinstance(value, dict):
            try:
                return pprint.pformat(value, sort_dicts=True, width=60).splitlines()
            except Exception:
                return [_safe_assert_repr(value)]
        if isinstance(value, (list, tuple)):
            try:
                return pprint.pformat(value, width=60).splitlines()
            except Exception:
                return [_safe_assert_repr(value)]
        return []

    if op != "==":
        return None
    if not isinstance(left, (dict, list, tuple, set, frozenset, str)):
        return None
    if type(left) is not type(right) and not (
        isinstance(left, (set, frozenset)) and isinstance(right, (set, frozenset))
    ):
        return None
    if isinstance(left, str) and ("\n" not in left and "\n" not in right):
        return None
    # Small dicts collapse to one pformat line; a per-key delta is far more
    # useful than replacing the whole line. Compute it BEFORE the expensive
    # pformat lines so a big dict does not pay for both renderings.
    if isinstance(left, dict) and isinstance(right, dict):
        try:
            added = sorted(set(right) - set(left), key=repr)
            removed = sorted(set(left) - set(right), key=repr)
            shared = set(left) & set(right)
            changed = []
            for k in shared:
                try:
                    if left[k] != right[k]:
                        changed.append(k)
                except Exception:
                    # Ambiguous truth (numpy) or broken __eq__: still show the pair.
                    changed.append(k)
            changed.sort(key=repr)
        except Exception:
            return None
        delta: list[str] = []
        for k in changed:
            delta.append(f"~ {k!r}: {_safe_assert_repr(left[k])} -> {_safe_assert_repr(right[k])}")
        for k in removed:
            delta.append(f"- {k!r}: {_safe_assert_repr(left[k])} (only in left)")
        for k in added:
            delta.append(f"+ {k!r}: {_safe_assert_repr(right[k])} (only in right)")
        if delta:
            max_delta = 80
            if len(delta) > max_delta:
                delta = delta[:max_delta] + [f"... {len(delta) - max_delta} more changed keys"]
            return "\n".join(delta)
    left_lines = _lines(left)
    right_lines = _lines(right)
    if not left_lines and not right_lines:
        return None
    diff = list(
        difflib.unified_diff(
            left_lines, right_lines, fromfile="left", tofile="right", lineterm="", n=2
        )
    )
    if not diff:
        return None
    max_lines = 80
    if len(diff) > max_lines:
        diff = diff[:max_lines] + [f"... diff truncated ({len(diff) - max_lines} more lines)"]
    return "\n".join(diff)


def _ztest_assert_format(idx: int, op: str, left: Any, right: Any, expr_src: str, msg: Any = None) -> str:
    # Called only on the failing branch — the message is lazy, like the native assert; idx is a diagnostic counter, not baked into the text. Invariants: REWRITE-PRESERVES-SCOPE.
    _ = idx
    lr = _safe_assert_repr(left)
    rr = _safe_assert_repr(right)
    if len(lr) > 300:
        lr = lr[:297] + "..."
    if len(rr) > 300:
        rr = rr[:297] + "..."
    text = f"assert {expr_src}\n  Left:  {lr}\n  Right: {rr}"
    if op == "==":
        # UX audit: drop the noisy ``(op: ==)`` suffix and, for containers and
        # multiline strings, append a structural diff instead of leaving the
        # user with two 300-char truncated reprs.
        diff = None
        try:
            diff = _ztest_structured_diff(op, left, right)
        except BaseException:
            # Diagnostics must never replace the user's AssertionError with a
            # helper failure (broken repr / hostile container / numpy truth).
            diff = None
        if diff is not None:
            text = f"{text}\n  Difference (left vs right):\n" + "\n".join(
                f"    {line}" for line in diff.splitlines()
            )
    else:
        text = f"{text} (op: {op})"
    if msg is not None:
        return f"{msg}\n{text}"
    return text


def _ztest_assert_bool(val: Any) -> bool:
    # Delegate truthiness to bool(val) so multi-element array truth raises ValueError (pytest parity). Invariants: ARRAY-TRUTH-FALLBACK, ALL-FALLBACK-SHAPE-GATED.
    return bool(val)


def _ztest_assert_val_format(val: Any, expr_src: str) -> str:
    vr = _safe_assert_repr(val)
    if len(vr) > 300:
        vr = vr[:297] + "..."
    return f"assert {expr_src}\n  where {expr_src} = {vr}"


def _ztest_assert_not_format(val: Any, operand_src: str, expr_src: str) -> str:
    vr = _safe_assert_repr(val)
    if len(vr) > 300:
        vr = vr[:297] + "..."
    return f"assert {expr_src}\n  where {operand_src} = {vr}"


def _ztest_assert_call_format(res: Any, fn_src: str, args_info: list[tuple[str, Any]], expr_src: str) -> str:
    rr = _safe_assert_repr(res)
    if len(rr) > 300:
        rr = rr[:297] + "..."
    if not args_info:
        return f"assert {expr_src}\n  where {rr} = {expr_src}"
    parts: list[str] = []
    for a_src, a_val in args_info:
        ar = _safe_assert_repr(a_val)
        if len(ar) > 200:
            ar = ar[:197] + "..."
        if not a_src:
            parts.append(ar)
        elif a_src.startswith("__kw__:"):
            parts.append(f"{a_src[7:]}={ar}")
        else:
            parts.append(f"{a_src}={ar}")
    sig = ", ".join(parts)
    return f"assert {expr_src}\n  where {rr} = {fn_src}({sig})"


def _ztest_assert_or_format(expr_src: str, branch_msgs: list[Any]) -> str:
    lines = [f"assert {expr_src}"]
    for bm in branch_msgs:
        if isinstance(bm, tuple) and bm and callable(bm[0]):
            bm = bm[0](*bm[1:])
        if isinstance(bm, str) and bm:
            for sub in bm.splitlines():
                lines.append("  " + sub)
    return "\n".join(lines)


def _ztest_assert_finalize(detail: str, top_src: str, msg: Any = None) -> str:
    header = f"assert {top_src}"
    if detail.startswith(header):
        text = detail
    else:
        text = f"{header}\n  ({detail})"
    if msg is not None:
        return f"{msg}\n{text}"
    return text


class _AssertRewriteTransformer(ast.NodeTransformer):
    _OP_MAP: dict[type[ast.cmpop], str] = {
        ast.Eq: "==",
        ast.NotEq: "!=",
        ast.Lt: "<",
        ast.LtE: "<=",
        ast.Gt: ">",
        ast.GtE: ">=",
        ast.In: "in",
        ast.NotIn: "not in",
        ast.Is: "is",
        ast.IsNot: "is not",
    }
    _MAGIC_CALLS: frozenset[str] = frozenset({"super", "locals", "globals", "vars", "dir", "eval", "exec"})

    def __init__(self) -> None:
        super().__init__()
        # REWRITE-PRESERVES-SCOPE: unique temp-name counter — __zt_l<N>/__zt_r<N> must never collide
        self._n = 0

    @staticmethod
    def _unparse(expr: ast.AST, fallback: str = "<expr>") -> str:
        try:
            return ast.unparse(expr)
        except Exception:
            return fallback

    def _new_temp(self, temps: list[str], prefix: str = "v") -> str:
        n = self._n
        self._n += 1
        name = f"__zt_{prefix}{n}"
        temps.append(name)
        return name

    def _can_rewrite_top(self, expr: ast.AST) -> bool:
        if isinstance(expr, ast.Compare):
            return len(expr.ops) >= 1 and len(expr.ops) == len(expr.comparators) and all(type(op) in self._OP_MAP for op in expr.ops)
        if isinstance(expr, ast.BoolOp):
            return isinstance(expr.op, (ast.And, ast.Or)) and len(expr.values) >= 2
        if isinstance(expr, ast.UnaryOp):
            return isinstance(expr.op, ast.Not)
        if isinstance(expr, ast.Call):
            return True
        return False

    @staticmethod
    def _make_fmt(lazy: bool, func_name: str, args: list[ast.expr]) -> ast.expr:
        fn = ast.Name(id=func_name, ctx=ast.Load())
        if lazy:
            return ast.Tuple(elts=[fn, *args], ctx=ast.Load())
        return ast.Call(func=fn, args=args, keywords=[])

    def _compile_cond(self, expr: ast.AST, fail_var: str, temps: list[str], lazy: bool = False) -> list[ast.stmt]:
        if isinstance(expr, ast.Compare) and len(expr.ops) >= 1 and len(expr.ops) == len(expr.comparators) and all(type(op) in self._OP_MAP for op in expr.ops):
            expr_src = self._unparse(expr, "<compare>")
            l_var = self._new_temp(temps, "l")
            r_vars = [self._new_temp(temps, "r") for _ in expr.ops]
            assign_l = ast.Assign(targets=[ast.Name(id=l_var, ctx=ast.Store())], value=expr.left)
            next_stmts: list[ast.stmt] = []
            for i in range(len(expr.ops) - 1, -1, -1):
                op_str = self._OP_MAP[type(expr.ops[i])]
                comp_node = expr.comparators[i]
                left_node_src = expr.left if i == 0 else expr.comparators[i - 1]
                step_src = expr_src if len(expr.ops) == 1 else f"{self._unparse(left_node_src)} {op_str} {self._unparse(comp_node)}"
                left_var = l_var if i == 0 else r_vars[i - 1]
                right_var = r_vars[i]
                assign_r = ast.Assign(targets=[ast.Name(id=right_var, ctx=ast.Store())], value=comp_node)
                pred = ast.Call(
                    func=ast.Name(id="__ztest_assert_cmp__", ctx=ast.Load()),
                    args=[
                        ast.Constant(value=op_str),
                        ast.Name(id=left_var, ctx=ast.Load()),
                        ast.Name(id=right_var, ctx=ast.Load()),
                    ],
                    keywords=[])
                fmt_call = self._make_fmt(
                    lazy,
                    "__ztest_assert_format__",
                    [
                        ast.Constant(value=0),
                        ast.Constant(value=op_str),
                        ast.Name(id=left_var, ctx=ast.Load()),
                        ast.Name(id=right_var, ctx=ast.Load()),
                        ast.Constant(value=step_src),
                        ast.Constant(value=None),
                    ])
                set_fail = ast.Assign(targets=[ast.Name(id=fail_var, ctx=ast.Store())], value=fmt_call)
                if_step = ast.If(
                    test=ast.UnaryOp(op=ast.Not(), operand=pred),
                    body=[set_fail],
                    orelse=next_stmts)
                next_stmts = [assign_r, if_step]
            return [assign_l, *next_stmts]

        if isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.And):
            next_and: list[ast.stmt] = []
            for v in reversed(expr.values):
                cur = self._compile_cond(v, fail_var, temps, lazy=lazy)
                if next_and:
                    guard = ast.If(
                        test=ast.Compare(
                            left=ast.Name(id=fail_var, ctx=ast.Load()),
                            ops=[ast.Is()],
                            comparators=[ast.Constant(value=None)]),
                        body=next_and,
                        orelse=[])
                    cur = [*cur, guard]
                next_and = cur
            return next_and

        if isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.Or):
            expr_src = self._unparse(expr, "<or>")
            sub_vars = [self._new_temp(temps, "or") for _ in expr.values]
            or_fail = ast.Assign(
                targets=[ast.Name(id=fail_var, ctx=ast.Store())],
                value=self._make_fmt(
                    lazy,
                    "__ztest_assert_or_format__",
                    [
                        ast.Constant(value=expr_src),
                        ast.List(elts=[ast.Name(id=sv, ctx=ast.Load()) for sv in sub_vars], ctx=ast.Load()),
                    ]))
            next_or: list[ast.stmt] = [or_fail]
            for i in range(len(expr.values) - 1, -1, -1):
                sv = sub_vars[i]
                init_sv = ast.Assign(targets=[ast.Name(id=sv, ctx=ast.Store())], value=ast.Constant(value=None))
                branch_stmts = self._compile_cond(expr.values[i], sv, temps, lazy=True)
                check_failed = ast.If(
                    test=ast.Compare(
                        left=ast.Name(id=sv, ctx=ast.Load()),
                        ops=[ast.IsNot()],
                        comparators=[ast.Constant(value=None)]),
                    body=next_or,
                    orelse=[])
                next_or = [init_sv, *branch_stmts, check_failed]
            return next_or

        if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not):
            expr_src = self._unparse(expr, "not <expr>")
            op_src = self._unparse(expr.operand, "<expr>")
            v_var = self._new_temp(temps, "v")
            assign_v = ast.Assign(targets=[ast.Name(id=v_var, ctx=ast.Store())], value=expr.operand)
            pred = ast.Call(
                func=ast.Name(id="__ztest_assert_bool__", ctx=ast.Load()),
                args=[ast.Name(id=v_var, ctx=ast.Load())],
                keywords=[])
            fmt = self._make_fmt(
                lazy,
                "__ztest_assert_not_format__",
                [
                    ast.Name(id=v_var, ctx=ast.Load()),
                    ast.Constant(value=op_src),
                    ast.Constant(value=expr_src),
                ])
            if_stmt = ast.If(
                test=pred,
                body=[ast.Assign(targets=[ast.Name(id=fail_var, ctx=ast.Store())], value=fmt)],
                orelse=[])
            return [assign_v, if_stmt]

        if isinstance(expr, ast.Call):
            expr_src = self._unparse(expr, "<call>()")
            fn_src = self._unparse(expr.func, "<fn>")
            can_split = (
                not (isinstance(expr.func, ast.Name) and expr.func.id in self._MAGIC_CALLS)
                and not any(isinstance(a, ast.Starred) for a in expr.args)
                and all(kw.arg is not None for kw in expr.keywords)
            )
            stmts: list[ast.stmt] = []
            info_elts: list[ast.expr] = []
            if can_split:
                fn_var = self._new_temp(temps, "fn")
                stmts.append(ast.Assign(targets=[ast.Name(id=fn_var, ctx=ast.Store())], value=expr.func))
                call_args: list[ast.expr] = []
                call_kws: list[ast.keyword] = []
                for a in expr.args:
                    a_var = self._new_temp(temps, "a")
                    stmts.append(ast.Assign(targets=[ast.Name(id=a_var, ctx=ast.Store())], value=a))
                    call_args.append(ast.Name(id=a_var, ctx=ast.Load()))
                    a_label = "" if isinstance(a, ast.Constant) else self._unparse(a, "arg")
                    info_elts.append(ast.Tuple(elts=[ast.Constant(value=a_label), ast.Name(id=a_var, ctx=ast.Load())], ctx=ast.Load()))
                for kw in expr.keywords:
                    kw_var = self._new_temp(temps, "kw")
                    stmts.append(ast.Assign(targets=[ast.Name(id=kw_var, ctx=ast.Store())], value=kw.value))
                    call_kws.append(ast.keyword(arg=kw.arg, value=ast.Name(id=kw_var, ctx=ast.Load())))
                    info_elts.append(ast.Tuple(elts=[ast.Constant(value=f"__kw__:{kw.arg}"), ast.Name(id=kw_var, ctx=ast.Load())], ctx=ast.Load()))
                res_var = self._new_temp(temps, "res")
                new_call = ast.Call(func=ast.Name(id=fn_var, ctx=ast.Load()), args=call_args, keywords=call_kws)
                stmts.append(ast.Assign(targets=[ast.Name(id=res_var, ctx=ast.Store())], value=new_call))
            else:
                res_var = self._new_temp(temps, "res")
                stmts.append(ast.Assign(targets=[ast.Name(id=res_var, ctx=ast.Store())], value=expr))
            pred = ast.Call(
                func=ast.Name(id="__ztest_assert_bool__", ctx=ast.Load()),
                args=[ast.Name(id=res_var, ctx=ast.Load())],
                keywords=[])
            fmt = self._make_fmt(
                lazy,
                "__ztest_assert_call_format__",
                [
                    ast.Name(id=res_var, ctx=ast.Load()),
                    ast.Constant(value=fn_src),
                    ast.List(elts=info_elts, ctx=ast.Load()),
                    ast.Constant(value=expr_src),
                ])
            stmts.append(ast.If(
                test=ast.UnaryOp(op=ast.Not(), operand=pred),
                body=[ast.Assign(targets=[ast.Name(id=fail_var, ctx=ast.Store())], value=fmt)],
                orelse=[]))
            return stmts

        expr_src = self._unparse(expr, "<expr>")
        v_var = self._new_temp(temps, "v")
        assign_v = ast.Assign(targets=[ast.Name(id=v_var, ctx=ast.Store())], value=expr)
        pred = ast.Call(
            func=ast.Name(id="__ztest_assert_bool__", ctx=ast.Load()),
            args=[ast.Name(id=v_var, ctx=ast.Load())],
            keywords=[])
        fmt = self._make_fmt(
            lazy,
            "__ztest_assert_val_format__",
            [
                ast.Name(id=v_var, ctx=ast.Load()),
                ast.Constant(value=expr_src),
            ])
        if_stmt = ast.If(
            test=ast.UnaryOp(op=ast.Not(), operand=pred),
            body=[ast.Assign(targets=[ast.Name(id=fail_var, ctx=ast.Store())], value=fmt)],
            orelse=[])
        return [assign_v, if_stmt]

    def visit_Assert(self, node: ast.Assert) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.test, ast.Compare) and len(node.test.ops) == 1 and len(node.test.comparators) == 1:
            op_type = type(node.test.ops[0])
            op_str = self._OP_MAP.get(op_type)
            if op_str is not None:
                expr_src = self._unparse(node.test, f"<compare {op_str}>")
                # Scope-preserving rewrite v2 without a lambda — the block lives in the enclosing scope, operands are evaluated once in source order, await/walrus are legal. Invariants: REWRITE-PRESERVES-SCOPE.
                n = self._n
                self._n += 1
                l_name = f"__zt_l{n}"
                r_name = f"__zt_r{n}"
                assign_l = ast.Assign(
                    targets=[ast.Name(id=l_name, ctx=ast.Store())],
                    value=node.test.left)
                assign_r = ast.Assign(
                    targets=[ast.Name(id=r_name, ctx=ast.Store())],
                    value=node.test.comparators[0])
                pred = ast.Call(
                    func=ast.Name(id="__ztest_assert_cmp__", ctx=ast.Load()),
                    args=[
                        ast.Constant(value=op_str),
                        ast.Name(id=l_name, ctx=ast.Load()),
                        ast.Name(id=r_name, ctx=ast.Load()),
                    ],
                    keywords=[])
                msg_arg: ast.AST = node.msg if node.msg is not None else ast.Constant(value=None)
                fmt_call = ast.Call(
                    func=ast.Name(id="__ztest_assert_format__", ctx=ast.Load()),
                    args=[
                        ast.Constant(value=n),
                        ast.Constant(value=op_str),
                        ast.Name(id=l_name, ctx=ast.Load()),
                        ast.Name(id=r_name, ctx=ast.Load()),
                        ast.Constant(value=expr_src),
                        msg_arg,
                    ],
                    keywords=[])
                raise_stmt = ast.Raise(
                    exc=ast.Call(
                        func=ast.Name(id="AssertionError", ctx=ast.Load()),
                        args=[fmt_call],
                        keywords=[]),
                    cause=None)
                if_stmt = ast.If(
                    test=ast.UnaryOp(op=ast.Not(), operand=pred),
                    body=[raise_stmt],
                    orelse=[])
                # Temporaries are cleaned up on the success path, otherwise they pin operands in the frame and falsely fail weakref/getrefcount tests. Invariants: REWRITE-TEMPORARIES-CLEARED.
                clear_stmt = ast.Assign(
                    targets=[
                        ast.Name(id=l_name, ctx=ast.Store()),
                        ast.Name(id=r_name, ctx=ast.Store()),
                    ],
                    value=ast.Constant(value=None))
                # Rewrite under `if __debug__:` — under -O operands are not evaluated, the walrus does not bind. Invariants: REWRITE-DEBUG-GATED.
                debug_guard = ast.If(
                    test=ast.Name(id="__debug__", ctx=ast.Load()),
                    body=[assign_l, assign_r, if_stmt, clear_stmt],
                    orelse=[])
                for built in (assign_l, assign_r, if_stmt, clear_stmt, debug_guard):
                    ast.copy_location(built, node)
                return [debug_guard]

        # Compound assert rewrite (chained Compare, BoolOp and/or, UnaryOp not, Call).
        # When an explicit message is supplied on a non-Compare assert (e.g. `assert fn(), "msg"`),
        # keep the native assert if it has no Compare children so callers matching exact str(AssertionError)
        # in unit tests continue to see the verbatim message.
        if self._can_rewrite_top(node.test):
            if node.msg is not None and not any(isinstance(sub, ast.Compare) for sub in ast.walk(node.test)):
                return node
            top_src = self._unparse(node.test, "<assert>")
            temps: list[str] = []
            fail_var = self._new_temp(temps, "fail")
            init_fail = ast.Assign(targets=[ast.Name(id=fail_var, ctx=ast.Store())], value=ast.Constant(value=None))
            cond_stmts = self._compile_cond(node.test, fail_var, temps)
            msg_arg = node.msg if node.msg is not None else ast.Constant(value=None)
            fin_call = ast.Call(
                func=ast.Name(id="__ztest_assert_finalize__", ctx=ast.Load()),
                args=[
                    ast.Name(id=fail_var, ctx=ast.Load()),
                    ast.Constant(value=top_src),
                    msg_arg,
                ],
                keywords=[])
            raise_stmt = ast.Raise(
                exc=ast.Call(
                    func=ast.Name(id="AssertionError", ctx=ast.Load()),
                    args=[fin_call],
                    keywords=[]),
                cause=None)
            check_fail = ast.If(
                test=ast.Compare(
                    left=ast.Name(id=fail_var, ctx=ast.Load()),
                    ops=[ast.IsNot()],
                    comparators=[ast.Constant(value=None)]),
                body=[raise_stmt],
                orelse=[])
            clear_stmt = ast.Assign(
                targets=[ast.Name(id=t, ctx=ast.Store()) for t in temps],
                value=ast.Constant(value=None))
            debug_guard = ast.If(
                test=ast.Name(id="__debug__", ctx=ast.Load()),
                body=[init_fail, *cond_stmts, check_fail, clear_stmt],
                orelse=[])
            for sub in ast.walk(debug_guard):
                if isinstance(sub, (ast.stmt, ast.expr)):
                    ast.copy_location(sub, node)
            return [debug_guard]
        return node


def _package_qualified_name(filepath: Path, fallback: str) -> str:
    """Qualified name via the __init__.py chain upward to the first
    directory without one (import root → sys.path/PYTHONPATH, parents via importlib).
    Outside a package — fallback. Identical stems in different packages are distinguished."""
    pkg_parts: list[str] = []
    walk = filepath.parent
    while (walk / "__init__.py").is_file():
        pkg_parts.insert(0, walk.name)
        walk = walk.parent
    if not pkg_parts:
        return fallback
    import_root = str(walk)
    if import_root not in sys.path:
        sys.path.insert(0, import_root)
    # Do not touch os.environ['PYTHONPATH'] (pytest parity: CLI subprocesses
    # see a clean environment); the import path lives only in the worker's sys.path.
    for i in range(1, len(pkg_parts) + 1):
        importlib.import_module(".".join(pkg_parts[:i]))
    return ".".join([*pkg_parts, filepath.stem])


def load_module_with_ast_cache(filepath: Path, module_name: str, cache_dir: Path | None = None) -> types.ModuleType:
    abs_path = str(filepath.resolve())
    src_bytes = filepath.read_bytes()
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}:{importlib.util.MAGIC_NUMBER.hex()}"
    # The -O level in the cache key and in compile() — otherwise collect-only under PYTHONOPTIMIZE caches code without asserts = fabricated PASS. Invariants: AST-CACHE-OPT-LEVEL.
    opt_level = str(sys.flags.optimize)
    h = hashlib.sha256()
    h.update(abs_path.encode("utf-8"))
    h.update(b"\x00")
    h.update(src_bytes)
    h.update(b"\x00")
    h.update(SHIM_VERSION.encode("utf-8"))
    h.update(b"\x00")
    h.update(py_ver.encode("utf-8"))
    h.update(b"\x00")
    h.update(opt_level.encode("utf-8"))
    h.update(b"\x00")
    # Compile flags are part of the cache key — changing dont_inherit and the
    # like must invalidate the cache even without a SHIM_VERSION bump.
    h.update(b"dont_inherit=1")
    digest = h.hexdigest()

    code_obj = None
    cache_file = (cache_dir / "ast" / f"{digest}.pyc") if cache_dir else None
    if cache_file and cache_file.exists():
        try:
            code_obj = marshal.loads(cache_file.read_bytes())
        except Exception:
            code_obj = None

    if code_obj is None:
        tree = ast.parse(src_bytes, filename=abs_path)
        tree = _AssertRewriteTransformer().visit(tree)
        ast.fix_missing_locations(tree)
        code_obj = compile(tree, filename=abs_path, mode="exec", optimize=sys.flags.optimize, dont_inherit=True)  # the shim's CO_FUTURE_ANNOTATIONS must not leak into user modules
        if cache_file:
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_bytes(marshal.dumps(code_obj))
            except Exception:
                pass

    # pkgutil.get_data(__name__, ...) on a module requires
    # __spec__.loader with get_data — SourceFileLoader, not loader=None
    # (otherwise pkgutil.get_data silently returns None).
    mod = types.ModuleType(module_name)
    mod.__file__ = abs_path
    mod.__package__ = module_name.rpartition(".")[0]
    _loader = importlib.machinery.SourceFileLoader(module_name, abs_path)
    mod.__spec__ = importlib.machinery.ModuleSpec(module_name, loader=_loader, origin=abs_path)
    mod.__loader__ = _loader
    mod.__dict__["__ztest_assert_cmp__"] = _ztest_assert_cmp
    mod.__dict__["__ztest_assert_format__"] = _ztest_assert_format
    mod.__dict__["__ztest_assert_bool__"] = _ztest_assert_bool
    mod.__dict__["__ztest_assert_val_format__"] = _ztest_assert_val_format
    mod.__dict__["__ztest_assert_not_format__"] = _ztest_assert_not_format
    mod.__dict__["__ztest_assert_call_format__"] = _ztest_assert_call_format
    mod.__dict__["__ztest_assert_or_format__"] = _ztest_assert_or_format
    mod.__dict__["__ztest_assert_finalize__"] = _ztest_assert_finalize
    sys.modules[module_name] = mod
    # The flush barrier: exec of a user module (test module/conftest) — a
    # long FILE_START/COLLECT window must not hide pending frames.
    _flush_ipc_now()
    try:
        exec(code_obj, mod.__dict__)
    except BaseException:
        # a half-initialized module must never linger in sys.modules — a later
        # import of that name would resurrect the broken object instead of re-executing it.
        sys.modules.pop(module_name, None)
        raise
    # The import machinery, on a normal import, registers the child module
    # as an attribute of the parent; the shim's manual loading skipped that
    # → logging.config resolve/ext:// (a getattr chain from the package)
    # could not find tests.test_x. Parity restored.
    if "." in module_name:
        _parent_name, _, _child_name = module_name.rpartition(".")
        _parent_mod = sys.modules.get(_parent_name)
        if _parent_mod is not None:
            try:
                setattr(_parent_mod, _child_name, mod)
            except Exception:
                pass
    return mod


