# Copyright (c) 2026 D.L <103242127@qq.com>
# Licensed under the MIT License.
#
# OrbitAI helper script (Windows / PowerShell)
# Usage:
#   .\bot.ps1 setup     # 创建 .venv + 装依赖
#   .\bot.ps1 start     # 后台启动 webui
#   .\bot.ps1 stop      # 停止 webui
#   .\bot.ps1 restart   # 重启
#   .\bot.ps1 status    # 状态
#   .\bot.ps1 logs      # 跟随日志
#   .\bot.ps1 run       # 前台启动（开发用）
#
# 如果遇到「无法加载文件……执行策略」错误，先运行：
#   Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned

param(
    [Parameter(Position=0)]
    [string]$Command = "help"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Port    = if ($env:PORT) { $env:PORT } else { 8765 }
$HostIp  = if ($env:HOST) { $env:HOST } else { "0.0.0.0" }
$Py      = ".venv\Scripts\python.exe"
$WebLog  = "logs\webui.log"

function _CheckEnv {
    if (-not (Test-Path ".env")) {
        Write-Host "[X] 找不到 .env，请先 copy .env.example .env 并填入凭证" -ForegroundColor Red
        exit 1
    }
    if (-not (Test-Path $Py)) {
        Write-Host "[X] 找不到 .venv (请先跑 .\bot.ps1 setup)" -ForegroundColor Red
        exit 1
    }
}

function _LoadEnv {
    Get-Content ".env" | ForEach-Object {
        if ($_ -match '^\s*#') { return }
        if ($_ -match '^\s*$') { return }
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $name  = $Matches[1]
            $value = $Matches[2].Trim().Trim('"').Trim("'")
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

function _GetWebPid {
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
                Select-Object -First 1
        return $conn.OwningProcess
    } catch {
        return $null
    }
}

function Cmd-Setup {
    Write-Host "[*] 创建 .venv" -ForegroundColor Cyan
    if (-not (Test-Path ".venv")) {
        python -m venv .venv
    }
    Write-Host "[*] 安装 orbitai 包 (editable)" -ForegroundColor Cyan
    & .\.venv\Scripts\pip.exe install -e .
    if (-not (Test-Path ".env")) {
        Copy-Item .env.example .env
        Write-Host "[!] 已创建 .env，请编辑后填入凭证" -ForegroundColor Yellow
    }
    Write-Host "[OK] 环境就绪。下一步：编辑 .env，然后 .\bot.ps1 start" -ForegroundColor Green
}

function Cmd-Start {
    _CheckEnv
    $pid = _GetWebPid
    if ($pid) {
        Write-Host "[!] 已在运行 (PID $pid)" -ForegroundColor Yellow
        return
    }
    if (-not (Test-Path "logs")) { New-Item -ItemType Directory -Path "logs" | Out-Null }
    _LoadEnv
    Write-Host "[*] 启动 webui http://${HostIp}:${Port}" -ForegroundColor Cyan
    $args = "-m uvicorn orbitai.web.app:app --host $HostIp --port $Port"
    $proc = Start-Process -FilePath $Py -ArgumentList $args `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $WebLog `
        -RedirectStandardError "logs\webui.err.log"
    # 等就绪
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 300
        $pid = _GetWebPid
        if ($pid) {
            Write-Host "[OK] 已启动 (PID $pid)" -ForegroundColor Green
            Write-Host "  日志: Get-Content -Tail 100 -Wait $WebLog"
            return
        }
    }
    Write-Host "[X] 启动失败，看 $WebLog" -ForegroundColor Red
    if (Test-Path $WebLog) { Get-Content -Tail 20 $WebLog }
    exit 1
}

function Cmd-Stop {
    $pid = _GetWebPid
    if (-not $pid) {
        Write-Host "[!] 未在运行" -ForegroundColor Yellow
        return
    }
    Write-Host "[*] 停止 webui (PID $pid)" -ForegroundColor Cyan
    Stop-Process -Id $pid -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 300
        if (-not (_GetWebPid)) {
            Write-Host "[OK] 已停止" -ForegroundColor Green
            return
        }
    }
    Write-Host "[!] 优雅退出超时，强杀" -ForegroundColor Yellow
    Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
    Write-Host "[OK] 已停止" -ForegroundColor Green
}

function Cmd-Restart {
    Cmd-Stop
    Start-Sleep -Seconds 1
    Cmd-Start
}

function Cmd-Status {
    $pid = _GetWebPid
    if ($pid) {
        Write-Host "● webui 运行中  PID=$pid  URL=http://${HostIp}:${Port}" -ForegroundColor Green
        if (Test-Path "bot.pid") {
            $bp = [int](Get-Content "bot.pid" -ErrorAction SilentlyContinue)
            $bproc = Get-Process -Id $bp -ErrorAction SilentlyContinue
            if ($bproc) {
                Write-Host "● bot   运行中  PID=$bp" -ForegroundColor Green
            } else {
                Write-Host "● bot   未运行  (pid 文件残留)" -ForegroundColor Yellow
            }
        } else {
            Write-Host "● bot   未运行" -ForegroundColor Yellow
        }
    } else {
        Write-Host "● webui 未运行" -ForegroundColor Red
    }
}

function Cmd-Logs {
    if (-not (Test-Path $WebLog)) {
        Write-Host "[!] 尚无日志文件 $WebLog" -ForegroundColor Yellow
        return
    }
    Get-Content -Tail 100 -Wait $WebLog
}

function Cmd-Run {
    _CheckEnv
    _LoadEnv
    Write-Host "[*] 前台启动 webui http://${HostIp}:${Port}  (Ctrl+C 退出)" -ForegroundColor Cyan
    & $Py -m uvicorn orbitai.web.app:app --host $HostIp --port $Port
}

function Cmd-Help {
@"
OrbitAI helper (Windows)

Usage: .\bot.ps1 <command>

Commands:
  setup     创建 .venv 并安装依赖
  start     后台启动 webui
  stop      停止 webui
  restart   重启 webui
  status    查看运行状态
  logs      实时跟随日志
  run       前台启动（开发用）
  help      显示本帮助

Environment overrides (PowerShell `):
  `$env:PORT=8765 ; `$env:HOST="0.0.0.0" ; .\bot.ps1 start
"@
}

switch ($Command.ToLower()) {
    "setup"   { Cmd-Setup }
    "start"   { Cmd-Start }
    "stop"    { Cmd-Stop }
    "restart" { Cmd-Restart }
    "status"  { Cmd-Status }
    "logs"    { Cmd-Logs }
    "run"     { Cmd-Run }
    "help"    { Cmd-Help }
    "-h"      { Cmd-Help }
    "--help"  { Cmd-Help }
    default {
        Write-Host "[X] 未知命令: $Command" -ForegroundColor Red
        Cmd-Help
        exit 1
    }
}
