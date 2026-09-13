<#
.SYNOPSIS
    FloatTrip 镜像构建与推送脚本（Windows PowerShell 版）

.DESCRIPTION
    一条命令完成：检查 Docker 环境 -> 构建镜像 -> 打标签 -> 登录并推送到 Docker Hub。
    所有步骤均带中文提示与失败中止，避免在凭据缺失时产生半成品推送。

.PARAMETER Image
    Docker Hub 镜像仓库名，默认 hotpot1993/floattrip

.PARAMETER Tag
    镜像标签，默认 latest

.PARAMETER Push
    显式开启推送。脚本在命名参数模式下默认会询问是否推送。

.PARAMETER NoPush
    只构建不推送，适合本地验证。

.PARAMETER Platform
    目标平台，例如 linux/amd64 或 linux/arm64；留空则使用当前平台。

.EXAMPLE
    .\build.ps1
    交互式构建并推送 hotpot1993/floattrip:latest

.EXAMPLE
    .\build.ps1 -NoPush
    只构建本地镜像，验证 Dockerfile 是否正确

.EXAMPLE
    .\build.ps1 -Platform linux/amd64 -Tag v1.0.0
    构建指定平台并打上 v1.0.0 标签后推送
#>

[CmdletBinding()]
param(
    [string]$Image = "hotpot1993/floattrip",
    [string]$Tag = "latest",
    [switch]$Push,
    [switch]$NoPush,
    [string]$Platform = ""
)

# 保证中文输出在控制台正常显示
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==== $Message ====" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Message)
    Write-Host "[成功] $Message" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host "[提示] $Message" -ForegroundColor Yellow
}

function Write-Err {
    param([string]$Message)
    Write-Host "[错误] $Message" -ForegroundColor Red
}

# ─────────────────────── 步骤 1：检查 Docker 环境 ───────────────────────
Write-Step "步骤 1/5：检查 Docker 环境"

$dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
if (-not $dockerCmd) {
    Write-Err "未找到 docker 命令。"
    Write-Host "请先安装并启动 Docker Desktop："
    Write-Host "  winget install --id Docker.DockerDesktop -e"
    Write-Host "安装完成后重启终端，确认 'docker version' 可正常输出。"
    exit 1
}

# 服务端信息可读，说明 Docker 守护进程已就绪（仅装 CLI 不够）
try {
    $serverVersion = (docker version --format '{{.Server.Version}}' 2>&1)
    if ($LASTEXITCODE -ne 0) { throw $serverVersion }
    Write-Ok "Docker 守护进程已就绪，服务端版本：$serverVersion"
} catch {
    Write-Err "Docker 守护进程未运行。请启动 Docker Desktop 后重试。"
    Write-Host "原始信息：$_"
    exit 1
}

# 确认在项目根目录执行（Dockerfile 与 requirements.txt 必须存在）
foreach ($required in @("Dockerfile", "requirements.txt", "run.py")) {
    if (-not (Test-Path $required)) {
        Write-Err "当前目录缺少 $required，请在项目根目录（FloatTrip）下运行本脚本。"
        exit 1
    }
}
Write-Ok "构建上下文校验通过：$(Get-Location)"

# ─────────────────────── 步骤 2：确认推送意图 ───────────────────────
Write-Step "步骤 2/5：确认操作范围"

$fullImage = "${Image}:${Tag}"
Write-Host "目标镜像：$fullImage"

$shouldPush = $false
if ($NoPush) {
    $shouldPush = $false
    Write-Warn "已指定 -NoPush，本次仅构建，不推送。"
} elseif ($Push) {
    $shouldPush = $true
} else {
    # 交互确认，避免误把本地验证镜像推到公共仓库
    $answer = Read-Host "构建完成后是否推送到 Docker Hub？(y/N)"
    $shouldPush = ($answer -eq "y" -or $answer -eq "Y")
}

# ─────────────────────── 步骤 3：构建镜像 ───────────────────────
Write-Step "步骤 3/5：构建镜像"

$buildArgs = @("build", "-t", $fullImage)
if ($Platform) {
    $buildArgs += @("--platform", $Platform)
}
$buildArgs += "."

Write-Host "执行：docker $($buildArgs -join ' ')"
& docker @buildArgs
if ($LASTEXITCODE -ne 0) {
    Write-Err "镜像构建失败，请查看上方构建日志定位问题。"
    exit 1
}
Write-Ok "镜像构建完成：$fullImage"

# 输出镜像体积，便于评估
$sizeInfo = (docker images $Image --format "{{.Repository}}:{{.Tag}}  {{.Size}}" 2>&1 | Select-Object -First 3)
if ($sizeInfo) {
    Write-Host "当前本地镜像："
    $sizeInfo | ForEach-Object { Write-Host "  $_" }
}

if (-not $shouldPush) {
    Write-Step "完成"
    Write-Ok "已跳过推送。本地运行验证命令："
    Write-Host "  docker run --rm -p 8765:8765 --env-file .env $fullImage"
    exit 0
}

# ─────────────────────── 步骤 4：登录 Docker Hub ───────────────────────
Write-Step "步骤 4/5：登录 Docker Hub"

Write-Host "即将执行 docker login。"
Write-Host "请在提示处输入 Docker Hub 用户名与密码/访问令牌（Access Token）。"
Write-Host "注意：Docker Hub 已于 2025 年起要求使用访问令牌替代账号密码登录。"

& docker login
if ($LASTEXITCODE -ne 0) {
    Write-Err "Docker Hub 登录失败，推送已中止。"
    exit 1
}
Write-Ok "Docker Hub 登录成功"

# ─────────────────────── 步骤 5：推送镜像 ───────────────────────
Write-Step "步骤 5/5：推送镜像到 Docker Hub"

Write-Host "执行：docker push $fullImage"
& docker push $fullImage
if ($LASTEXITCODE -ne 0) {
    Write-Err "镜像推送失败。"
    Write-Host "常见原因："
    Write-Host "  1. Docker Hub 中不存在仓库 $Image，需先在网页端创建（建议设为 Public）"
    Write-Host "  2. 登录账号无权写入该仓库（用户名拼写错误）"
    Write-Host "  3. 网络受限，可配置国内镜像加速或调整代理"
    exit 1
}

Write-Ok "推送完成：$fullImage"
Write-Host ""
Write-Host "在任意机器上拉取运行："
Write-Host "  docker run -d --name floattrip -p 8765:8765 --env-file .env $fullImage"
Write-Host ""
Write-Host "Docker Hub 页面：https://hub.docker.com/r/$Image"
