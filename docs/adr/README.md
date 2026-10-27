# Architecture decision records

ADR numbers are permanent keys: they are never renumbered and never reused.
Records 001-003 covered designs that were replaced before the first public
release and are not part of this tree; the surviving records keep their
original numbers.

## Index

- [ADR-004](ADR-004-process-containment.md) — process containment: three
  layers, what each covers, and where they overlap on purpose.
- [ADR-005](ADR-005-unittest-delegation.md) — delegating the
  `unittest.TestCase` lifecycle to the real runner instead of imitating it.
- [ADR-006](ADR-006-layout.md) — modular shim sources, one embedded runtime
  bundle (accepted v2), and the dependency-weight rule for `main.zig`.
