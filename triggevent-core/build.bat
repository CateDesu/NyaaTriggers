@echo off
REM Build triggevent-core on Windows. Requires JDK 17 + Maven on PATH.
REM   winget install EclipseAdoptium.Temurin.17.JDK
REM   winget install Apache.Maven      (or: scoop install maven)
REM Produces: triggevent-core\target\triggevent-core.jar
setlocal enabledelayedexpansion

set "HERE=%~dp0"
if not defined EVENT_TRIGGER_DIR set "EVENT_TRIGGER_DIR=%HERE%event-trigger"
if not defined EVENT_TRIGGER_REPO set "EVENT_TRIGGER_REPO=https://github.com/xpdota/event-trigger.git"
REM Pinned to a specific event-trigger commit so the vendored patches\ apply
REM deterministically, same as build.sh. Bump both scripts together.
if not defined EVENT_TRIGGER_REF set "EVENT_TRIGGER_REF=43bcf52782922360daf66bfb57e22d9251111a0e"

where java >nul 2>nul || (echo ERROR: JDK 17 not found - run: winget install EclipseAdoptium.Temurin.17.JDK & exit /b 1)
where mvn  >nul 2>nul || (echo ERROR: Maven not found - run: winget install Apache.Maven & exit /b 1)
where git  >nul 2>nul || (echo ERROR: git not found. & exit /b 1)

if not exist "%EVENT_TRIGGER_DIR%\.git" (
  echo ^>^> cloning event-trigger into %EVENT_TRIGGER_DIR%
  git clone "%EVENT_TRIGGER_REPO%" "%EVENT_TRIGGER_DIR%" || exit /b 1
  git -C "%EVENT_TRIGGER_DIR%" checkout %EVENT_TRIGGER_REF% || exit /b 1
) else (
  echo ^>^> reusing existing clone at %EVENT_TRIGGER_DIR%
  REM Re-assert the pin, a reused clone may have drifted.
  for /f %%i in ('git -C "%EVENT_TRIGGER_DIR%" rev-parse HEAD') do set "ET_HEAD=%%i"
  if not "!ET_HEAD!"=="%EVENT_TRIGGER_REF%" (
    echo ^>^> existing clone is not at the pinned ref; checking out %EVENT_TRIGGER_REF%
    git -C "%EVENT_TRIGGER_DIR%" checkout %EVENT_TRIGGER_REF% || exit /b 1
  )
)

REM Apply the vendored engine patches on top of the pinned source, same as
REM build.sh: skip if already applied, fail the build if one applies neither
REM way. A jar without the DMU crash guards must never ship.
if exist "%HERE%patches" (
  for %%p in ("%HERE%patches\*.patch") do (
    git -C "%EVENT_TRIGGER_DIR%" apply --reverse --check "%%p" >nul 2>nul
    if !errorlevel! equ 0 (
      echo ^>^> patch already applied: %%~nxp
    ) else (
      git -C "%EVENT_TRIGGER_DIR%" apply --check "%%p" >nul 2>nul
      if !errorlevel! equ 0 (
        echo ^>^> applying patch: %%~nxp
        git -C "%EVENT_TRIGGER_DIR%" apply "%%p" || exit /b 1
      ) else (
        echo ERROR: patch does not apply cleanly, event-trigger drifted from the pin: %%~nxp
        echo Refusing to build a jar without the vendored engine guards. Re-check patches\ or bump the pin.
        exit /b 1
      )
    )
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
