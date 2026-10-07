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

Engine contract checks protect recovery ordering, engine lifecycle, the Cactbot
timeline gate and native Triggernometry speech controls:

```bash
python3 -m tests test_engine_contracts test_triggernometry_host
```

`test_engine_contracts` uses real Qt signals, queues and timeline parsing with a
controlled clock and simulated Triggevent capabilities. It does not launch Java
or test a live IINACT feed. `test_triggernometry_host` runs the committed native
host under Mono and Xvfb. Its speech control check uses the production bridge,
executes C# scripts and waits for real delayed actions. These checks protect
behavior rather than a particular implementation.

`test_callout_hot_paths` checks cheap trigger rejection, zone-cache freshness,
repeated native speech through queued Qt delivery, and background session writes.
Storage checks block a real writer while combat dispatch continues, then verify
shutdown persistence, retries, bounded queues and superseded writes. They also
keep saved recaps reachable when a queued summary fails. UI tests wait explicitly
for background saves before reading their files.

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

The Triggernometry host suite needs Mono and Xvfb on Linux. The TTS recovery
suite requires NumPy. CI installs these dependencies and fails if they are
missing. Some other suites skip unavailable native dependencies locally.
The runner's completion count includes suites with skips, so check their output
for coverage.

Pytest collects only the suites listed in `conftest.py`. Use `python3 -m tests`
for a complete run because many scripts perform their checks during import.

After building the engine jar, verify its diagnostic protocol with:

```bash
python3 triggevent-core/test_diagnostics.py
```

The engine fork's `CrossFightFaultInjectionTest` replays UMAD, M1S, M2S and FRU
with missing or delayed mechanic inputs. It compares callout text, timing and
counts against recorded expectations, including local deaths and raises.
The release build runs those tests and the sequential lifecycle and UMAD suites
against the pinned fork source before publishing the engine.
It also requires the listener registration race tests, which verify that a reload
retains both manual callbacks and newly loaded component handlers.

Automarker checks use recorded encounter inputs and loopback Telesto endpoints:

```bash
python3 -m tests test_automarker_pipeline test_gaze_replay
python3 triggevent-core/test_automarkers.py
python3 triggevent-core/test_automarker_bridge.py
```

The two native checks require a built engine jar. They verify HTTP ordering,
rejection, timeouts, lifecycle cancellation, native settings replay and exact
party targets through the production bridge. The fork also checks UWU, DSR,
TOP, UMAD and marker state with duplicate, missing and reordered inputs. Required
native classes must run without skips before release. These endpoints never
submit game commands to a live plugin. HTTP acceptance cannot confirm a visible
in-game marker.

`test_triggevent_recovery` checks unexpected engine exits on a healthy feed,
retry backoff, startup and recovery deadlines, ordered buffered input and stale
acknowledgements. `test_connection_audit` repeats the healthy feed exit with a
real JVM and loopback WebSocket. Native checks run separately after building
the jar so the clean Python release test does not depend on local Java artifacts.

`test_tts_recovery` checks cancellation during neural synthesis and model loading,
fresh speech after interruption and a bound on abandoned work. The separate
Linux `triggevent-core/test_pipeline.py` check carries recorded UMAD callouts through
the Java process, Qt, synthesis and a captured audio subprocess. It compares
complete speech lists and fault cases with saved expectations. Its voice and
audio sink are test doubles, so it does not test physical audibility.
