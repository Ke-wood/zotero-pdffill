@echo off
setlocal enabledelayedexpansion

rem zotero-pdffill entry: pdffill.bat ^<scan^|fast^|fetch^|attach^|report^|run^> [args...]
rem fetched/ and logs/ go to .\work unless PDFFILL_ROOT is set.

set "HERE=%~dp0"
set "HERE=%HERE:~0,-1%"

if not defined PDFFILL_ROOT set "PDFFILL_ROOT=%HERE%\work"
rem export to child process
set "PDFFILL_ROOT=%PDFFILL_ROOT%"
if not exist "%PDFFILL_ROOT%" mkdir "%PDFFILL_ROOT%" 2>nul

set "CMD=%~1"
if "%CMD%"=="" goto usage

rem collect remaining args after the subcommand
set "REST="
shift
:collect
if "%~1"=="" goto haveargs
set "REST=!REST! %1"
shift
goto collect
:haveargs

if /i "%CMD%"=="fast" (
  python "%HERE%\pdffill_fast.py"!REST!
  goto end
)
if /i "%CMD%"=="scan"   goto domean
if /i "%CMD%"=="fetch"  goto domean
if /i "%CMD%"=="attach" goto domean
if /i "%CMD%"=="report" goto domean
if /i "%CMD%"=="run"    goto domean
goto usage

:domean
python "%HERE%\pdffill.py" %CMD%!REST!
goto end

:usage
echo.
echo   zotero-pdffill -- batch fill missing PDFs in Zotero
echo.
echo   pdffill.bat scan                        list items missing PDF
echo   pdffill.bat scan --collection NAME      only one collection
echo   pdffill.bat fast --workers 14           concurrent fetch (recommended)
echo   pdffill.bat fetch                       serial fetch
echo   pdffill.bat attach                      attach to Zotero (quit Zotero first)
echo   pdffill.bat report                      coverage stats
echo   pdffill.bat run --collection NAME       all-in-one
echo.
echo   work dir : %PDFFILL_ROOT%
echo   Zotero DB: auto-detected (override with ZOTERO_DB_PATH)
echo.

:end
endlocal
