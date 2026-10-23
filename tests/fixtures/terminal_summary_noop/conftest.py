def pytest_terminal_summary(terminalreporter):
    # target/conftest.py: a purely reporting hook; ztest prints no pytest
    # terminal summary, so the hook is deliberately not invoked and must not
    # break collection (cf. pytest_report_header).
    if terminalreporter.stats.get("failed"):
        terminalreporter.write_sep(" ", "DO *NOT* COMMIT!", red=True, bold=True)
