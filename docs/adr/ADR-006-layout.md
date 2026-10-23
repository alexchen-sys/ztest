# ADR-006: repository layout — what is a module and what is not

Status: accepted. Supersedes nothing; records the reasoning behind the file
layout a reader sees at 0.5.0.

## Context

Two files dominate the tree: `src/main.zig` (~5.5k lines) and
`src/shim/ztest_shim.py` (~7.9k lines). Both sizes invite the same question,
so the reasoning is recorded once, here.

## Decision

**The shim stays one file.** It is embedded into the supervisor binary with
`@embedFile` and deployed into zygote workers as a single self-contained
script; a test that prints a traceback must see one file name, and the
anti-drift test compiles exactly the embedded bytes. Splitting it would mean a
concatenation step in `build.zig` — a new build failure mode bought with no
runtime benefit. The file is instead organized as marked sections with short
functions (median length ~20 lines).

**`src/main.zig` is split by dependency weight, not by size target.** A
section moves out only when its closure over module state is empty, so the
move is text motion with alias re-exports and cannot change behavior. Under
that rule the configuration scanners (`src/pyfiles.zig`: `python_files` /
`norecursedirs` from ini/pyproject) and the command-line parser
(`src/cli.zig`, with the timeout helpers) moved out; both depend only on
`std`. The output channel, discovery, and the IPC frame layer stay in
`main.zig` because they share the output/globals boundary; `runMain` (the
rolling dispatcher and the containment calls) stays because it holds ~45
run-scoped locals and the ADR-004 invariants interlock across its phases —
extracting it safely wants a supervisor struct first.

## Consequences

The tree reads as: `main.zig` (supervisor core + native tests),
`pyfiles.zig`, `cli.zig`, `spawn_arm.zig`, `shim/ztest_shim.py`. Reviewers can
weigh the containment core directly; configuration and CLI details live in
their own files; the shim's single-file shape is a deployment property, not an
oversight.
