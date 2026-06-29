#Requires -Version 5.1
<#
.SYNOPSIS
  One-click local GPU worker setup for ComfyUI + Pixelle-Video on Windows.

.DESCRIPTION
  1. Creates D:\content-ops folders
  2. Downloads preconfigured bundle from your server
  3. Checks ComfyUI (8188) and Pixelle-Video API (8000)
  4. Runs the video worker once (or loops if -Daemon)

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File setup_windows.ps1
  powershell -ExecutionPolicy Bypass -File setup_windows.ps1 -Daemon
#>
param(
    [string]$ServerHost = "118.25.178.116",
    [string]$BaseDir = "D:\content-ops",
    [string]$RepoDir = "",
    [switch]$Daemon,
    [switch]$SkipHealthCheck
)

$ErrorActionPreference = "Stop"
$DeployUrl = "http://${ServerHost}:8787/local-gpu-deploy"

function Write-Step($msg) { Write-Host "[*] $msg" -ForegroundColor Cyan }
function Write-Ok($msg) { Write-Host "[OK] $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "[WARN] $msg" -ForegroundColor Yellow }

Write-Step "Creating directories under $BaseDir"
$dirs = @(
    $BaseDir,
    "$BaseDir\video_jobs\pending",
    "$BaseDir\video_jobs\completed",
    "$BaseDir\video_jobs\failed",
    "$BaseDir\rendered_videos",
    "$BaseDir\ssh",
    "$BaseDir\scripts"
)
foreach ($d in $dirs) { New-Item -ItemType Directory -Force -Path $d | Out-Null }

Write-Step "Downloading deploy bundle from $DeployUrl"
$bundleFiles = @(
    "local_config.json",
    "worker.py",
    "start_worker.bat",
    "worker_key"
)
foreach ($name in $bundleFiles) {
    $out = Join-Path $BaseDir $name
    if ($name -eq "worker_key") { $out = Join-Path "$BaseDir\ssh" "worker_key" }
    if ($name -eq "worker.py" -or $name -eq "start_worker.bat") { $out = Join-Path "$BaseDir\scripts" $name }
    if ($name -eq "local_config.json") { $out = Join-Path $BaseDir "local_config.json" }
    Invoke-WebRequest -Uri "$DeployUrl/$name" -OutFile $out -UseBasicParsing
    Write-Ok "Downloaded $name"
}

$keyPath = "$BaseDir\ssh\worker_key"
icacls $keyPath /inheritance:r /grant:r "$env:USERNAME`:F" | Out-Null

$configPath = "$BaseDir\local_config.json"
$config = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
$config.jobs_dir = "$BaseDir\video_jobs".Replace("\", "/")
$config.output_dir = "$BaseDir\rendered_videos".Replace("\", "/")
$config.upload.ssh_identity_file = $keyPath.Replace("\", "/")
$config.pull_jobs_from_server.ssh_identity_file = $keyPath.Replace("\", "/")
$config | ConvertTo-Json -Depth 8 | Set-Content -Path $configPath -Encoding UTF8

if (-not $SkipHealthCheck) {
    Write-Step "Checking ComfyUI at $($config.comfyui_url)"
    try {
        Invoke-WebRequest -Uri $config.comfyui_url -UseBasicParsing -TimeoutSec 5 | Out-Null
        Write-Ok "ComfyUI is reachable"
    } catch {
        Write-Warn "ComfyUI not reachable. Please start ComfyUI first (port 8188)."
    }

    Write-Step "Checking Pixelle-Video API at $($config.pixelle_api_url)"
    try {
        $health = Invoke-WebRequest -Uri "$($config.pixelle_api_url)/api/health" -UseBasicParsing -TimeoutSec 5
        Write-Ok "Pixelle-Video API is reachable"
    } catch {
        Write-Warn "Pixelle-Video API not reachable. Start with: uv run uvicorn api.app:app --host 0.0.0.0 --port 8000"
    }
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
if (-not $python) { throw "Python not found. Install Python 3.10+ and add to PATH." }

$worker = "$BaseDir\scripts\worker.py"
$args = @($worker, "--config", $configPath)
if (-not $Daemon) { $args += "--once" }

Write-Step "Starting local GPU worker"
& $python.Source @args
Write-Ok "Setup complete. Rendered videos: $BaseDir\rendered_videos"
Write-Ok "Preview portal: http://${ServerHost}:8787/index.html"
