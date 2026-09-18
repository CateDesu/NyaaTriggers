@echo off
REM Build target/triggevent-core.jar with JDK 17 and Maven on PATH.
setlocal enabledelayedexpansion

set "HERE=%~dp0"
if not defined EVENT_TRIGGER_DIR set "EVENT_TRIGGER_DIR=%HERE%event-trigger"
if not defined EVENT_TRIGGER_REPO set "EVENT_TRIGGER_REPO=https://github.com/CateDesu/event-trigger.git"
REM Keep the engine commit pin in sync with build.sh.
if not defined EVENT_TRIGGER_REF set "EVENT_TRIGGER_REF=06015a947b5b8c7d67f4863b8033e1c14e185494"

where java >nul 2>nul || (echo ERROR: JDK 17 not found - run: winget install EclipseAdoptium.Temurin.17.JDK & exit /b 1)
where mvn  >nul 2>nul || (echo ERROR: Maven not found - run: winget install Apache.Maven & exit /b 1)
where git  >nul 2>nul || (echo ERROR: git not found. & exit /b 1)

if not exist "%EVENT_TRIGGER_DIR%\.git" (
  echo ^>^> cloning event-trigger into %EVENT_TRIGGER_DIR%
  git clone "%EVENT_TRIGGER_REPO%" "%EVENT_TRIGGER_DIR%" || exit /b 1
  git -C "%EVENT_TRIGGER_DIR%" checkout %EVENT_TRIGGER_REF% || exit /b 1
) else (
  echo ^>^> reusing existing clone at %EVENT_TRIGGER_DIR%
  REM Update older checkouts to use the engine fork.
  for /f %%i in ('git -C "%EVENT_TRIGGER_DIR%" remote get-url origin 2^>nul') do set "ET_ORIGIN=%%i"
  if not "!ET_ORIGIN!"=="%EVENT_TRIGGER_REPO%" (
    echo ^>^> repointing origin at %EVENT_TRIGGER_REPO%
    git -C "%EVENT_TRIGGER_DIR%" remote add origin "%EVENT_TRIGGER_REPO%" 2>nul || git -C "%EVENT_TRIGGER_DIR%" remote set-url origin "%EVENT_TRIGGER_REPO%" || exit /b 1
  )
  REM Fetch when the pinned commit is missing locally.
  git -C "%EVENT_TRIGGER_DIR%" cat-file -e "%EVENT_TRIGGER_REF%^{commit}" 2>nul
  if errorlevel 1 (
    echo ^>^> fetching %EVENT_TRIGGER_REPO%
    git -C "%EVENT_TRIGGER_DIR%" fetch origin || exit /b 1
  )
  for /f %%i in ('git -C "%EVENT_TRIGGER_DIR%" rev-parse HEAD') do set "ET_HEAD=%%i"
  if not "!ET_HEAD!"=="%EVENT_TRIGGER_REF%" (
    echo ^>^> existing clone is not at the pinned ref; checking out %EVENT_TRIGGER_REF%
    git -C "%EVENT_TRIGGER_DIR%" checkout %EVENT_TRIGGER_REF% || exit /b 1
  )
)

echo ^>^> installing Triggevent Engine modules to local Maven repo
pushd "%EVENT_TRIGGER_DIR%"
call mvn -q -Dmaven.test.skip=true -pl :actimport,:xivsupport,:trigger-support,:triggers-general,:triggers-ew,:triggers-sb,:triggers-dt,:titan-jails,:easytriggers,:timelines,:telesto-core -am clean install || (popd & exit /b 1)
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
