@echo off
echo ============================================
echo   CommandCenter - Download FFmpeg ^& MediaMTX
echo ============================================
echo.
echo This script downloads portable binaries to the bin\ folder.
echo Run this ONCE before using the app.
echo.

set BIN_DIR=%~dp0bin
if not exist "%BIN_DIR%" mkdir "%BIN_DIR%"

REM ---- FFmpeg ----
set FFMPEG_URL=https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip
set FFMPEG_ZIP=%BIN_DIR%\ffmpeg.zip

if exist "%BIN_DIR%\ffmpeg.exe" (
    echo [OK] FFmpeg already exists, skipping.
) else (
    echo [1/2] Downloading FFmpeg...

    powershell -Command "& { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%FFMPEG_URL%' -OutFile '%FFMPEG_ZIP%' -UseBasicParsing }"

    if not exist "%FFMPEG_ZIP%" (
        echo [ERROR] Failed to download FFmpeg. Check your internet connection.
        goto :mediamtx
    )

    echo        Extracting FFmpeg...
    powershell -Command "& { Add-Type -AssemblyName System.IO.Compression.FileSystem; $zip = [System.IO.Compression.ZipFile]::OpenRead('%FFMPEG_ZIP%'); foreach ($e in $zip.Entries) { if ($e.Name -eq 'ffmpeg.exe' -or $e.Name -eq 'ffprobe.exe') { $dest = Join-Path '%BIN_DIR%' $e.Name; [System.IO.Compression.ZipFileExtensions]::ExtractToFile($e, $dest, $true) } }; $zip.Dispose() }"

    del "%FFMPEG_ZIP%" 2>nul

    if exist "%BIN_DIR%\ffmpeg.exe" (
        echo [OK] FFmpeg installed successfully.
    ) else (
        echo [ERROR] FFmpeg extraction failed.
    )
)

:mediamtx
REM ---- MediaMTX ----
if exist "%BIN_DIR%\mediamtx.exe" (
    echo [OK] MediaMTX already exists, skipping.
) else (
    echo [2/2] Downloading MediaMTX...

    REM Get latest release URL from GitHub API
    powershell -Command "& { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; $rel = Invoke-RestMethod -Uri 'https://api.github.com/repos/bluenviron/mediamtx/releases/latest' -UseBasicParsing; $asset = $rel.assets | Where-Object { $_.name -match 'windows.*amd64.*\.zip$' } | Select-Object -First 1; if ($asset) { Invoke-WebRequest -Uri $asset.browser_download_url -OutFile '%BIN_DIR%\mediamtx.zip' -UseBasicParsing; Write-Host $asset.name } else { Write-Host 'NOT_FOUND' } }"

    if not exist "%BIN_DIR%\mediamtx.zip" (
        echo [ERROR] Failed to download MediaMTX. Check your internet connection.
        goto :done
    )

    echo        Extracting MediaMTX...
    powershell -Command "& { Add-Type -AssemblyName System.IO.Compression.FileSystem; $zip = [System.IO.Compression.ZipFile]::OpenRead('%BIN_DIR%\mediamtx.zip'); foreach ($e in $zip.Entries) { if ($e.Name -eq 'mediamtx.exe') { $dest = Join-Path '%BIN_DIR%' $e.Name; [System.IO.Compression.ZipFileExtensions]::ExtractToFile($e, $dest, $true) } }; $zip.Dispose() }"

    del "%BIN_DIR%\mediamtx.zip" 2>nul

    if exist "%BIN_DIR%\mediamtx.exe" (
        echo [OK] MediaMTX installed successfully.
    ) else (
        echo [ERROR] MediaMTX extraction failed.
    )
)

:done
echo.
echo ============================================
echo   Setup complete! You can now run start.bat
echo ============================================
pause
