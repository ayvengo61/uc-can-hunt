@echo off
chcp 65001 >nul
title Установка uc-can-hunt
setlocal EnableDelayedExpansion
echo ==========================================================
echo   uc-can-hunt: подготовка компьютера для работы с Claude
echo ==========================================================
echo.
echo Шаг 1 из 4. Проверяю Python...
set "PY="
py -3 -c "print('ok')" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "print('ok')" >nul 2>&1 && set "PY=python"
if not defined PY (
    echo.
    echo   Python не найден. Сейчас откроется сайт python.org.
    echo   Скачайте установщик, запустите его и ОБЯЗАТЕЛЬНО поставьте галочку
    echo   "Add python.exe to PATH" на первом экране. После установки запустите
    echo   этот файл ещё раз.
    start "" "https://www.python.org/downloads/windows/"
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('%PY% --version 2^>^&1') do echo   найден %%v

echo.
echo Шаг 2 из 4. Ставлю пакеты numpy и pillow (нужны для анализа записей)...
%PY% -m pip install --quiet --disable-pip-version-check --no-warn-script-location numpy pillow
if errorlevel 1 (
    echo   Не удалось установить пакеты. Проверьте интернет и запустите файл ещё раз.
    pause
    exit /b 1
)
echo   пакеты установлены.

echo.
echo Шаг 3 из 4. Проверяю Claude Code...
set "PATH=%PATH%;%USERPROFILE%\.local\bin"
claude --version >nul 2>&1
if errorlevel 1 (
    echo   Claude Code не найден. Ставлю официальным установщиком Anthropic...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex"
    set "PATH=%PATH%;%USERPROFILE%\.local\bin"
    claude --version >nul 2>&1
    if errorlevel 1 (
        echo.
        echo   Установить Claude Code автоматически не вышло. Поставьте его вручную:
        echo   https://claude.ai/code  и запустите этот файл ещё раз.
        echo   Напоминание: нужен платный план Claude ^(Pro или Max^) или оплата через API.
        start "" "https://claude.ai/code"
        pause
        exit /b 1
    )
)
for /f "delims=" %%v in ('claude --version 2^>^&1') do echo   найден Claude Code %%v

echo.
echo Шаг 4 из 4. Подключаю плагин uc-can-hunt...
claude plugin marketplace add ayvengo61/uc-can-hunt >nul 2>&1
claude plugin marketplace update uc-tools >nul 2>&1
claude plugin install uc-can-hunt@uc-tools
if errorlevel 1 (
    echo.
    echo   Плагин не подключился. Если Claude Code ещё не входил в аккаунт, выполните
    echo   в терминале:  claude   ^(войдите^), затем запустите этот файл ещё раз.
    pause
    exit /b 1
)

echo.
echo ==========================================================
echo   Готово. Что дальше:
echo   1. Запустите монитор своей платы ^(папка monitor^) и нажмите
echo      "Совместная работа".
echo   2. Откройте Claude Code и напишите, что ищете, например:
echo      "поехали, ищем обороты".
echo   Если Claude Code открыт сейчас, перезапустите его, чтобы увидеть плагин.
echo ==========================================================
echo.
pause
exit /b 0
