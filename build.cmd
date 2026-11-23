@echo off
rem Wrapper so build.ps1 runs without changing the PowerShell execution policy.
rem Usage: build [-Bump patch^|minor^|major] [-Install] [-NoInstaller] [-Clean]
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*
