# Tests

From the repository root, run all suites with:

```bash
python3 -m tests
```

The runner uses a separate Python process for each suite and defaults Qt to
offscreen mode. This keeps module patches and Qt state from leaking between
suites. The release workflow uses the same runner.
Replay tests keep automatic support logging disabled. Diagnostic suites write to
temporary files and check both useful evidence and excluded personal information.

Run selected suites with:

```bash
python3 -m tests test_prog_phases test_session_ui
```

The Prog comparison suites cover phase eligibility and the real session page.
`test_prog_pull_ui` checks saved-session search, bookmark navigation, copied
observations, editor history, and recovery after a session file cannot be replaced.
Archiving checks cover restart, saved recap navigation, comparison visibility,
and failed writes while retaining the latest notes.

For an individual suite, including its own command line options:

```bash
QT_QPA_PLATFORM=offscreen python3 -m tests.test_prog_phases -v
```

Use the program's Python dependencies. The plugin link, download deadline, and
Telesto resilience suites also need permission to open local sockets.

The Triggernometry host suite needs Mono and Xvfb on Linux. The NumPy checks
in the TTS suite need NumPy. CI installs these dependencies and fails if they
are missing. Local runs report skips when they are unavailable. The runner's
completion count includes suites with skips, so check their output for coverage.

Pytest collects only the suites listed in `conftest.py`. Use `python3 -m tests`
for a complete run because many scripts perform their checks during import.

After building the engine jar, verify its diagnostic protocol with:

```bash
python3 triggevent-core/test_diagnostics.py
```

The engine fork's `CrossFightFaultInjectionTest` replays UMAD, M1S, M2S and FRU
with missing or delayed mechanic inputs. It compares callout text, timing and
counts against recorded expectations, including local deaths and raises.
