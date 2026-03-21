@echo off
REM ================================================================
REM  fast_capture.dll build script
REM ================================================================

echo.
echo ============================================
echo   Building fast_capture.dll
echo ============================================
echo.

set "VCVARS=C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvarsall.bat"

if not exist "%VCVARS%" (
    echo [ERROR] vcvarsall.bat not found at:
    echo   %VCVARS%
    pause
    exit /b 1
)

echo [1/3] Setting up Visual Studio environment...
call "%VCVARS%" x64
if errorlevel 1 (
    echo [ERROR] Environment setup failed
    pause
    exit /b 1
)

echo [2/3] Compiling...
cl /LD /O2 /EHsc /MT fast_capture.cpp /link d3d11.lib dxgi.lib /OUT:fast_capture.dll
if errorlevel 1 (
    echo.
    echo [ERROR] Compilation failed. Check error messages above.
    pause
    exit /b 1
)

echo [3/3] Cleaning up...
del /q fast_capture.obj 2>nul
del /q fast_capture.lib 2>nul
del /q fast_capture.exp 2>nul

echo.
echo ============================================
echo   SUCCESS! fast_capture.dll created
echo ============================================
echo.
echo Next: python test_native_capture.py
echo.
pause
