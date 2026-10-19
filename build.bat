@echo off
rem Build all228x_factory_test_tool_v5.exe (single file) with PyInstaller.
rem ASCII only on purpose: cmd misparses UTF-8 multibyte text in .bat files.
rem Packages are installed into .venv-build only; system Python is untouched.
setlocal
cd /d "%~dp0"

echo [1/4] Checking Python...
py -3 --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install from https://www.python.org/downloads/windows/
    echo        and check "Add python.exe to PATH" / "py launcher".
    goto :fail
)
py -3 --version

if not exist "all228x_factory_test_tool_v5.py" (
    echo ERROR: all228x_factory_test_tool_v5.py not found in this folder.
    goto :fail
)

echo [2/4] Creating build venv (.venv-build)...
if not exist ".venv-build\Scripts\python.exe" (
    py -3 -m venv .venv-build || goto :fail
)

echo [3/4] Installing pyinstaller + paramiko into venv (pinned versions)...
rem Versions pinned from the first successful build (build_versions.txt).
".venv-build\Scripts\python.exe" -m pip install --upgrade pip || goto :fail
".venv-build\Scripts\python.exe" -m pip install ^
    altgraph==0.17.5 ^
    bcrypt==5.0.0 ^
    cffi==2.1.1 ^
    cryptography==50.0.2 ^
    invoke==3.0.3 ^
    packaging==26.3 ^
    paramiko==5.0.0 ^
    pefile==2024.8.26 ^
    pycparser==3.11 ^
    pyinstaller==6.22.3 ^
    pyinstaller-hooks-contrib==2026.8 ^
    PyNaCl==1.6.2 ^
    pywin32-ctypes==0.2.3 ^
    setuptools==84.0.0 ^
    || goto :fail
".venv-build\Scripts\python.exe" -m pip freeze > build_versions.txt

echo [4/4] Building single exe...
".venv-build\Scripts\python.exe" -m PyInstaller --onefile --windowed --clean ^
    --name all228x_factory_test_tool_v5 all228x_factory_test_tool_v5.py || goto :fail

echo.
echo DONE: dist\all228x_factory_test_tool_v5.exe
pause
exit /b 0

:fail
echo.
echo BUILD FAILED. Please copy the messages above.
pause
exit /b 1
