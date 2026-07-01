#Requires -Version 5.1
<#
.SYNOPSIS
  One-click local ComfyUI worker setup on Windows.
#>
param(
    [string]$ServerHost = "118.25.178.116",
    [string]$BaseDir = "D:\content-ops",
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
    "$BaseDir\workflows",
    "$BaseDir\ssh",
    "$BaseDir\scripts"
)
foreach ($d in $dirs) { New-Item -ItemType Directory -Force -Path $d | Out-Null }

Write-Step "Downloading deploy bundle from $DeployUrl"
$bundleFiles = @(
    "local_config.json",
    "worker.py",
    "comfyui_client.py",
    "slideshow_renderer.py",
    "start_worker.bat",
    "install_render_deps.bat",
    "short_video.api.json",
    "worker_key",
    "workflows_README.txt"
)
foreach ($name in $bundleFiles) {
    $out = Join-Path $BaseDir $name
    if ($name -eq "worker_key") { $out = Join-Path "$BaseDir\ssh" "worker_key" }
    if ($name -in @("worker.py", "comfyui_client.py", "slideshow_renderer.py", "start_worker.bat", "install_render_deps.bat")) {
        $out = Join-Path "$BaseDir\scripts" $name
    }
    if ($name -eq "local_config.json") { $out = Join-Path $BaseDir "local_config.json" }
    if ($name -eq "short_video.api.json") { $out = Join-Path "$BaseDir\workflows" "short_video.api.json" }
    if ($name -eq "workflows_README.txt") { $out = Join-Path "$BaseDir\workflows" "README.txt" }
    Invoke-WebRequest -Uri "$DeployUrl/$name" -OutFile $out -UseBasicParsing
    Write-Ok "Downloaded $name"
}

$keyPath = "$BaseDir\ssh\worker_key"
icacls $keyPath /inheritance:r /grant:r "$env:USERNAME`:F" | Out-Null

$configPath = "$BaseDir\local_config.json"
$config = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
$config.jobs_dir = "$BaseDir\video_jobs".Replace("\", "/")
$config.output_dir = "$BaseDir\rendered_videos".Replace("\", "/")
$config.comfyui.workflow_dir = "$BaseDir\workflows".Replace("\", "/")
$config.upload.ssh_identity_file = $keyPath.Replace("\", "/")
$config.pull_jobs_from_server.ssh_identity_file = $keyPath.Replace("\", "/")
$config | ConvertTo-Json -Depth 8 | Set-Content -Path $configPath -Encoding UTF8

if (-not $SkipHealthCheck) {
    $comfyUrl = $config.comfyui.url
    if (-not $comfyUrl) { $comfyUrl = $config.comfyui_url }
    Write-Step "Checking ComfyUI at $comfyUrl"
    try {
        Invoke-WebRequest -Uri "$comfyUrl/system_stats" -UseBasicParsing -TimeoutSec 5 | Out-Null
        Write-Ok "ComfyUI is reachable"
    } catch {
        Write-Warn "ComfyUI not reachable. Start ComfyUI on port 8000 first."
    }

    $workflow = Join-Path "$BaseDir\workflows" "short_video.api.json"
    if (Test-Path $workflow) {
        Write-Ok "Built-in workflow ready: $workflow"
    } else {
        Write-Warn "Missing built-in workflow file"
    }
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
if (-not $python) { throw "Python not found. Install Python 3.10+ and add to PATH." }

$worker = "$BaseDir\scripts\worker.py"
$args = @($worker, "--config", $configPath)
if (-not $Daemon) { $args += "--once" }

Write-Step "Starting ComfyUI worker"
& $python.Source @args
Write-Ok "Done. Videos: $BaseDir\rendered_videos"
Write-Ok "Preview: http://${ServerHost}:8787/index.html"
