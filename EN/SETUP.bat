@echo off
chcp 65001 >nul
title uc-can-hunt setup
setlocal EnableDelayedExpansion
echo ==========================================================
echo   uc-can-hunt: preparing this computer to work with Claude
echo ==========================================================
echo.
echo Step 1 of 4. Checking Python...
set "PY="
py -3 -c "print('ok')" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "print('ok')" >nul 2>&1 && set "PY=python"
if not defined PY (
    echo.
    echo   Python was not found. The python.org website will open now.
    echo   Download the installer, run it and MAKE SURE to tick
    echo   "Add python.exe to PATH" on the first screen. After installing,
    echo   run this file again.
    start "" "https://www.python.org/downloads/windows/"
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('%PY% --version 2^>^&1') do echo   found %%v

echo.
echo Step 2 of 4. Installing the numpy and pillow packages (needed to analyse recordings)...
%PY% -m pip install --quiet --disable-pip-version-check --no-warn-script-location numpy pillow
if errorlevel 1 (
    echo   Package installation failed. Check your internet connection and run this file again.
    pause
    exit /b 1
)
echo   packages installed.

echo.
echo Step 3 of 4. Checking Claude Code...
set "PATH=%PATH%;%USERPROFILE%\.local\bin"
claude --version >nul 2>&1
if errorlevel 1 (
    echo   Claude Code was not found. Installing it with the official Anthropic installer...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex"
    set "PATH=%PATH%;%USERPROFILE%\.local\bin"
    claude --version >nul 2>&1
    if errorlevel 1 (
        echo.
        echo   Automatic installation of Claude Code did not work. Install it manually:
        echo   https://claude.ai/code  and run this file again.
        echo   Reminder: a paid Claude plan ^(Pro or Max^) or API billing is required.
        start "" "https://claude.ai/code"
        pause
        exit /b 1
    )
)
for /f "delims=" %%v in ('claude --version 2^>^&1') do echo   found Claude Code %%v

echo.
echo Step 4 of 4. Connecting the uc-can-hunt plugin...
claude plugin marketplace add ayvengo61/uc-can-hunt >nul 2>&1
claude plugin marketplace update uc-tools >nul 2>&1
claude plugin install uc-can-hunt@uc-tools
if errorlevel 1 (
    echo.
    echo   The plugin could not be connected. If Claude Code has not signed in yet, run
    echo   claude  in a terminal ^(sign in^), then run this file again.
    pause
    exit /b 1
)

echo.
echo ==========================================================
echo   Done. What next:
echo   1. Start the monitor of your board ^(the monitor folder^) and click
echo      "Совместная работа" ^(collaboration^).
echo   2. Open Claude Code and say what you are looking for, for example:
echo      "let's find RPM".
echo   If Claude Code is open right now, restart it to see the plugin.
echo ==========================================================
echo.
pause
exit /b 0
