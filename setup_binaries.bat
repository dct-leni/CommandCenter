@echo off
cd /d "%~dp0"
echo ============================================
echo   CommandCenter - Setup Prerequisites
echo ============================================
echo.
echo This script installs Python 3.14 (if missing) and downloads
echo portable binaries (FFmpeg, MediaMTX, WireGuard) to bin\.
echo Run this ONCE before using the app.
echo.

REM ---- Python 3.10+ ----
echo [0/4] Checking for Python 3.10 or newer...
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>nul
if %ERRORLEVEL% equ 0 (
    echo [OK] Python 3.10 or newer is already installed.
) else (
    echo [INFO] Python 3.10+ not found. Installing Python 3.14 via winget...
    winget install -e --id Python.Python.3.14 --accept-package-agreements --accept-source-agreements
    
    if %ERRORLEVEL% equ 0 (
        echo [OK] Python 3.14 installed successfully. 
        echo      NOTE: You may need to restart your terminal after this script finishes to update your PATH.
    ) else (
        echo [ERROR] Failed to install Python 3.14 via winget. Please install it manually.
    )
)
echo.

set BIN_DIR=%~dp0bin
if not exist "%BIN_DIR%" mkdir "%BIN_DIR%"

REM ---- FFmpeg / FFprobe ----
set FFMPEG_URL=https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-win64-gpl-8.1.zip
set FFMPEG_ZIP=%BIN_DIR%\ffmpeg.zip

REM Only skip the download if BOTH ffmpeg.exe and ffprobe.exe are present.
if exist "%BIN_DIR%\ffmpeg.exe" if exist "%BIN_DIR%\ffprobe.exe" (
    echo [OK] FFmpeg and FFprobe already exist, skipping.
    goto :mediamtx
)

if exist "%BIN_DIR%\ffmpeg.exe" (
    echo [INFO] FFmpeg found but FFprobe is missing. Re-downloading to get ffprobe.exe...
) else (
    echo [1/4] Downloading FFmpeg...
)

powershell -Command "& { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%FFMPEG_URL%' -OutFile '%FFMPEG_ZIP%' -UseBasicParsing }"

if not exist "%FFMPEG_ZIP%" (
    echo [ERROR] Failed to download FFmpeg. Check your internet connection.
    goto :mediamtx
)

echo         Extracting FFmpeg and FFprobe...
powershell -Command "& { Add-Type -AssemblyName System.IO.Compression.FileSystem; $zip = [System.IO.Compression.ZipFile]::OpenRead('%FFMPEG_ZIP%'); foreach ($e in $zip.Entries) { if ($e.Name -eq 'ffmpeg.exe' -or $e.Name -eq 'ffprobe.exe') { $dest = Join-Path '%BIN_DIR%' $e.Name; [System.IO.Compression.ZipFileExtensions]::ExtractToFile($e, $dest, $true) } }; $zip.Dispose() }"

del "%FFMPEG_ZIP%" 2>nul

if exist "%BIN_DIR%\ffmpeg.exe" (
    echo [OK] FFmpeg installed successfully.
) else (
    echo [ERROR] FFmpeg extraction failed.
)

if exist "%BIN_DIR%\ffprobe.exe" (
    echo [OK] FFprobe installed successfully.
) else (
    echo [ERROR] FFprobe extraction failed - the downloaded build may not include it.
)

:mediamtx
REM ---- MediaMTX ----
if exist "%BIN_DIR%\mediamtx.exe" (
    echo [OK] MediaMTX already exists, skipping.
) else (
    echo [2/4] Downloading MediaMTX...

    REM Download MediaMTX v1.19.2 release directly
    powershell -Command "& { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://github.com/bluenviron/mediamtx/releases/download/v1.19.2/mediamtx_v1.19.2_windows_amd64.zip' -OutFile '%BIN_DIR%\mediamtx.zip' -UseBasicParsing }"

    if not exist "%BIN_DIR%\mediamtx.zip" (
        echo [ERROR] Failed to download MediaMTX. Check your internet connection.
        goto :wireguard
    )

    echo         Extracting MediaMTX...
    powershell -Command "& { Add-Type -AssemblyName System.IO.Compression.FileSystem; $zip = [System.IO.Compression.ZipFile]::OpenRead('%BIN_DIR%\mediamtx.zip'); foreach ($e in $zip.Entries) { if ($e.Name -eq 'mediamtx.exe') { $dest = Join-Path '%BIN_DIR%' $e.Name; [System.IO.Compression.ZipFileExtensions]::ExtractToFile($e, $dest, $true) } }; $zip.Dispose() }"

    del "%BIN_DIR%\mediamtx.zip" 2>nul

    if exist "%BIN_DIR%\mediamtx.exe" (
        echo [OK] MediaMTX installed successfully.
    ) else (
        echo [ERROR] MediaMTX extraction failed.
    )
)

:wireguard
REM ---- WireGuard Proxy ----
if exist "%BIN_DIR%\wireproxy.exe" (
    echo [OK] WireGuard proxy wireproxy.exe already exists in bin\, skipping.
) else (
    echo [3/3] Downloading WireGuard proxy wireproxy...
    powershell -Command "& { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://github.com/windtf/wireproxy/releases/download/v1.1.3/wireproxy_windows_amd64.tar.gz' -OutFile '%BIN_DIR%\wireproxy.tar.gz' -UseBasicParsing; tar -xzf '%BIN_DIR%\wireproxy.tar.gz' -C '%BIN_DIR%'; Remove-Item '%BIN_DIR%\wireproxy.tar.gz' -ErrorAction SilentlyContinue }"

    if exist "%BIN_DIR%\wireproxy.exe" (
        echo [OK] WireGuard proxy wireproxy.exe installed successfully in bin\.
    ) else (
        echo [WARNING] Failed to download wireproxy.exe.
    )
)

:done
echo.
echo ============================================
echo   Setup complete! You can now run start.bat
echo ============================================
pause