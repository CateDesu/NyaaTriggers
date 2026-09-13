# Tests

From the repository root, run all suites with:

```bash
python3 -m tests
```

The runner uses a separate Python process for each suite and defaults Qt to
offscreen mode. This keeps module patches and Qt state from leaking between
suites. The release workflow uses the same runner.

Run selected suites with:

```bash
python3 -m tests test_prog_phases test_session_ui
```

For an individual suite, including its own command line options:

```bash
QT_QPA_PLATFORM=offscreen python3 -m tests.test_prog_phases -v
```

Use the program's Python dependencies. The plugin link, download deadline, and
Telesto resilience suites also need permission to open local sockets.

Pytest collects only the suites listed in `conftest.py`. Use `python3 -m tests`
for a complete run because many scripts perform their checks during import.
