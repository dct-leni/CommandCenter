@echo off
cd /d "%~dp0"
echo ============================================
echo   CommandCenter - Setup Prerequisites
echo ============================================
echo.
echo This script installs Python 3.10+ (if missing) and downloads
echo portable binaries (FFmpeg, MediaMTX, WireGuard) to bin\.
echo Run this ONCE before using the app.
echo.

REM ---- Python 3.10+ ----
echo [0/6] Checking for Python 3.10 or newer...
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

if exist "%BIN_DIR%\ffmpeg.exe" if exist "%BIN_DIR%\ffprobe.exe" (
    echo [OK] FFmpeg and FFprobe already exist, skipping.
    goto :mediamtx
)

if exist "%BIN_DIR%\ffmpeg.exe" (
    echo [INFO] FFmpeg found but FFprobe is missing. Re-downloading to get ffprobe.exe...
) else (
    echo [1/6] Downloading FFmpeg...
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
    echo [ERROR] FFprobe extraction failed.
)

:mediamtx
REM ---- MediaMTX ----
if exist "%BIN_DIR%\mediamtx.exe" (
    echo [OK] MediaMTX already exists, skipping.
) else (
    echo [2/6] Downloading MediaMTX...
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
    echo [3/6] Downloading WireGuard proxy wireproxy...
    powershell -Command "& { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://github.com/windtf/wireproxy/releases/download/v1.1.3/wireproxy_windows_amd64.tar.gz' -OutFile '%BIN_DIR%\wireproxy.tar.gz' -UseBasicParsing; tar -xzf '%BIN_DIR%\wireproxy.tar.gz' -C '%BIN_DIR%'; Remove-Item '%BIN_DIR%\wireproxy.tar.gz' -ErrorAction SilentlyContinue }"

    if exist "%BIN_DIR%\wireproxy.exe" (
        echo [OK] WireGuard proxy wireproxy.exe installed successfully in bin\.
    ) else (
        echo [WARNING] Failed to download wireproxy.exe.
    )
)

:firefox
REM ---- Portable Firefox (phyrox-portable) ----
set FIREFOX_DIR=%BIN_DIR%\phyrox-portable-win64-152.0.4-70
set FIREFOX_EXE=%FIREFOX_DIR%\app\firefox.exe

if exist "%FIREFOX_EXE%" goto :firefox_exists
if exist "%FIREFOX_DIR%\phyrox-portable.exe" goto :firefox_exists
if exist "%FIREFOX_DIR%\firefox.exe" goto :firefox_exists
if exist "%BIN_DIR%\firefox-win\firefox.exe" goto :firefox_exists
if exist "%BIN_DIR%\firefox-win\app\firefox.exe" goto :firefox_exists
goto :download_firefox

:firefox_exists
echo [OK] Portable Firefox already exists, skipping.
goto :vbcable

:download_firefox

echo [5/6] Downloading Portable Firefox (phyrox-portable v152.0.4-70)...
set FIREFOX_7Z=%BIN_DIR%\phyrox-portable.7z
powershell -Command "& { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://github.com/portapps/phyrox-portable/releases/download/152.0.4-70/phyrox-portable-win64-152.0.4-70.7z' -OutFile '%FIREFOX_7Z%' -UseBasicParsing }"

if not exist "%FIREFOX_7Z%" (
    echo [WARNING] Failed to download Portable Firefox. Web streams will be unavailable.
    goto :vbcable
)

echo         Extracting Portable Firefox...
where 7z >nul 2>&1
if %ERRORLEVEL% == 0 (
    7z x "%FIREFOX_7Z%" -o"%BIN_DIR%" -y >nul
) else (
    powershell -Command "& { $p = '%FIREFOX_7Z%'; $d = '%BIN_DIR%'; try { & 7z x $p -o$d -y 2>&1 | Out-Null } catch { Write-Host 'No 7z found — install 7-Zip from https://www.7-zip.org/ then re-run setup.' }; }"
)

del "%FIREFOX_7Z%" 2>nul

if exist "%FIREFOX_EXE%" (
    echo [OK] Portable Firefox installed successfully.
) else (
    echo [WARNING] Firefox extraction may have failed. If so, install 7-Zip (https://www.7-zip.org/)
    echo          and re-run setup_binaries.bat, or manually extract to bin\phyrox-portable-win64-152.0.4-70\
)

:vbcable
REM ---- VB-Cable & SoundVolumeView ----
echo [6/6] Checking audio routing prerequisites...
sc query VBAudioVACWDM >nul 2>&1
if %ERRORLEVEL% == 0 (
    echo [OK] VB-Cable is already installed.
    goto :svv
)

echo        VB-Cable routes web stream audio to a silent virtual speaker so
echo        your physical speakers stay quiet while DRM audio is captured.
echo.

set VBCABLE_ZIP=%BIN_DIR%\vbcable.zip
set VBCABLE_DIR=%BIN_DIR%\vbcable-setup

powershell -Command "& { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack43.zip' -OutFile '%VBCABLE_ZIP%' -UseBasicParsing }"

if exist "%VBCABLE_ZIP%" (
    powershell -Command "Expand-Archive '%VBCABLE_ZIP%' -DestinationPath '%VBCABLE_DIR%' -Force"
    del "%VBCABLE_ZIP%" 2>nul
)

if exist "%VBCABLE_DIR%\VBCABLE_Setup_x64.exe" (
    echo         Installing VB-Cable — an Administrator prompt will appear...
    powershell -Command "Start-Process '%VBCABLE_DIR%\VBCABLE_Setup_x64.exe' -Verb RunAs -Wait"
    sc query VBAudioVACWDM >nul 2>&1
    if %ERRORLEVEL% == 0 (
        echo [OK] VB-Cable installed successfully!
    ) else (
        echo [INFO] VB-Cable installer completed. A reboot may be required to activate driver.
    )
)

:svv
if not exist "%BIN_DIR%\SoundVolumeView.exe" (
    echo [INFO] Downloading SoundVolumeView audio router...
    powershell -Command "& { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://www.nirsoft.net/utils/soundvolumeview-x64.zip' -OutFile '%BIN_DIR%\svv.zip' -UseBasicParsing }"
    powershell -Command "Expand-Archive '%BIN_DIR%\svv.zip' -DestinationPath '%BIN_DIR%' -Force"
    del "%BIN_DIR%\svv.zip" 2>nul
)

:cleanup
REM ---- Clean residual archives and doc files ----
del "%BIN_DIR%\*.zip" "%BIN_DIR%\*.7z" "%BIN_DIR%\*.tar.gz" "%BIN_DIR%\*.chm" "%BIN_DIR%\readme.txt" "%BIN_DIR%\README*" "%BIN_DIR%\LICENSE" 2>nul

:done
echo.
echo ============================================
echo   Setup complete! You can now run start.bat
echo ============================================
pause