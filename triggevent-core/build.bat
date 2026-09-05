@echo off
REM Build triggevent-core on Windows. Requires JDK 17 + Maven on PATH.
REM   winget install EclipseAdoptium.Temurin.17.JDK
REM   winget install Apache.Maven      (or: scoop install maven)
REM Produces: triggevent-core\target\triggevent-core.jar
setlocal enabledelayedexpansion

set "HERE=%~dp0"
if not defined EVENT_TRIGGER_DIR set "EVENT_TRIGGER_DIR=%HERE%event-trigger"
if not defined EVENT_TRIGGER_REPO set "EVENT_TRIGGER_REPO=https://github.com/CateDesu/event-trigger.git"
REM Pinned to a commit on the fork's guards branch, which carries the engine
REM guards as real commits. Bump both scripts together.
if not defined EVENT_TRIGGER_REF set "EVENT_TRIGGER_REF=2491d56d3ed66c79085a78fd1090dc0f3bbb3409"

where java >nul 2>nul || (echo ERROR: JDK 17 not found - run: winget install EclipseAdoptium.Temurin.17.JDK & exit /b 1)
where mvn  >nul 2>nul || (echo ERROR: Maven not found - run: winget install Apache.Maven & exit /b 1)
where git  >nul 2>nul || (echo ERROR: git not found. & exit /b 1)

if not exist "%EVENT_TRIGGER_DIR%\.git" (
  echo ^>^> cloning event-trigger into %EVENT_TRIGGER_DIR%
  git clone "%EVENT_TRIGGER_REPO%" "%EVENT_TRIGGER_DIR%" || exit /b 1
  git -C "%EVENT_TRIGGER_DIR%" checkout %EVENT_TRIGGER_REF% || exit /b 1
) else (
  echo ^>^> reusing existing clone at %EVENT_TRIGGER_DIR%
  REM Older clones point origin at upstream. The engine comes from the fork now.
  for /f %%i in ('git -C "%EVENT_TRIGGER_DIR%" remote get-url origin 2^>nul') do set "ET_ORIGIN=%%i"
  if not "!ET_ORIGIN!"=="%EVENT_TRIGGER_REPO%" (
    echo ^>^> repointing origin at %EVENT_TRIGGER_REPO%
    git -C "%EVENT_TRIGGER_DIR%" remote set-url origin "%EVENT_TRIGGER_REPO%" || exit /b 1
  )
  REM Re-assert the pin, a reused clone may have drifted.
  for /f %%i in ('git -C "%EVENT_TRIGGER_DIR%" rev-parse HEAD') do set "ET_HEAD=%%i"
  if not "!ET_HEAD!"=="%EVENT_TRIGGER_REF%" (
    echo ^>^> existing clone is not at the pinned ref; checking out %EVENT_TRIGGER_REF%
    git -C "%EVENT_TRIGGER_DIR%" checkout %EVENT_TRIGGER_REF% || exit /b 1
  )
)

echo ^>^> installing Triggevent Engine modules to local Maven repo
pushd "%EVENT_TRIGGER_DIR%"
call mvn -q -Dmaven.test.skip=true -pl :xivsupport,:trigger-support,:triggers-general,:triggers-ew,:triggers-sb,:triggers-dt,:titan-jails,:easytriggers,:timelines -am clean install || (popd & exit /b 1)
popd

echo ^>^> building triggevent-core.jar
pushd "%HERE%"
call mvn -q -Dmaven.test.skip=true clean package || (popd & exit /b 1)
popd

echo.
echo Built: %HERE%target\triggevent-core.jar
echo Note: Windows/macOS have no Xvfb, so Triggevent's own overlays may appear on screen.
echo       Disable them in your Triggevent overlay settings if you only want the callouts.
endlocal
