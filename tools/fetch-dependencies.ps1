<#
.SYNOPSIS
    Downloads the third-party binaries this project needs at runtime.

.DESCRIPTION
    FFmpeg and Rhubarb are executables, not source: they are not committed to
    the repository. Committing them would add roughly 190 MB to every clone, and
    the 3D model library would push a clone past 3 GB. GitHub also refuses any
    single file above 100 MB, so vendoring is not an option at this size.

    Run this once after cloning. It puts:
        tools/ffmpeg/ffmpeg.exe
        tools/rhubarb/Rhubarb-Lip-Sync-*/rhubarb.exe

    If FFmpeg is already on your PATH, you can skip it -- the pipeline calls it
    through the system binary when the local copy is missing.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools/fetch-dependencies.ps1

.NOTES
    Nothing here is required to read the code or run the test suite; only to
    transcode and render video.
#>

$ErrorActionPreference = 'Stop'
$root   = Split-Path -Parent $PSScriptRoot
$ffDir  = Join-Path $root 'tools\ffmpeg'
$rhDir  = Join-Path $root 'tools\rhubarb'

function Get-File($Url, $Destination) {
    Write-Host "  downloading $Url" -ForegroundColor Cyan
    $tmp = "$Destination.part"
    Invoke-WebRequest -Uri $Url -OutFile $tmp -UseBasicParsing
    Move-Item -Force $tmp $Destination
}

# ---------------------------------------------------------------- FFmpeg
if (Test-Path (Join-Path $ffDir 'ffmpeg.exe')) {
    Write-Host "FFmpeg already present - skipping." -ForegroundColor Green
} else {
    Write-Host "FFmpeg (~80 MB)" -ForegroundColor Yellow
    New-Item -ItemType Directory -Force -Path $ffDir | Out-Null
    $zip = Join-Path $env:TEMP 'ffmpeg-essentials.zip'
    Get-File 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' $zip
    Expand-Archive -Path $zip -DestinationPath (Join-Path $env:TEMP 'ffx') -Force
    $found = Get-ChildItem (Join-Path $env:TEMP 'ffx') -Recurse -Filter 'ffmpeg.exe' |
             Select-Object -First 1
    if (-not $found) { throw "ffmpeg.exe not found inside the archive" }
    Copy-Item $found.FullName (Join-Path $ffDir 'ffmpeg.exe') -Force
    Remove-Item $zip -Force
    Remove-Item (Join-Path $env:TEMP 'ffx') -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "FFmpeg installed." -ForegroundColor Green
}

# ---------------------------------------------------------------- Rhubarb
if (Get-ChildItem $rhDir -Recurse -Filter 'rhubarb.exe' -ErrorAction SilentlyContinue) {
    Write-Host "Rhubarb already present - skipping." -ForegroundColor Green
} else {
    Write-Host "Rhubarb Lip Sync (~80 MB)" -ForegroundColor Yellow
    $zip = Join-Path $env:TEMP 'rhubarb.zip'
    Get-File 'https://github.com/DanielSWolf/rhubarb-lip-sync/releases/download/v1.13.0/rhubarb-lip-sync-1.13.0-Windows.zip' $zip
    New-Item -ItemType Directory -Force -Path $rhDir | Out-Null
    Expand-Archive -Path $zip -DestinationPath $rhDir -Force
    Remove-Item $zip -Force
    Write-Host "Rhubarb installed." -ForegroundColor Green
}

Write-Host ''
Write-Host 'Dependencies ready. Start the server with launch.bat' -ForegroundColor Cyan