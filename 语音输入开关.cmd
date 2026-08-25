@echo off
rem cc-voice toggle: starts the daemon if stopped, stops it if running.
rem The floating island is the indicator - it appears when on, vanishes when off.
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0tools\toggle.ps1"
