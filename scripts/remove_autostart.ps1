# -*- coding: utf-8 -*-
<#
.SYNOPSIS
    取消 Agent（受控端）的开机自启。

.DESCRIPTION
    删除启动文件夹里的快捷方式。也可以顺便结束正在运行的 Agent 进程。

.PARAMETER KillRunning
    同时结束正在运行的 ALSPD-Agent 进程。

.PARAMETER Status
    只查看当前自启状态与运行状态，不做任何修改。

.EXAMPLE
    .\scripts\remove_autostart.ps1
    .\scripts\remove_autostart.ps1 -Status
    .\scripts\remove_autostart.ps1 -KillRunning
#>
param(
    [switch]$KillRunning,
    [switch]$Status
)

$ErrorActionPreference = 'Stop'
$shortcutName = 'ALSPD-Agent.lnk'
$startupDir = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupDir $shortcutName

Write-Host ""
Write-Host ("=" * 74)
Write-Host "  ALSPD-DESK  Agent 开机自启"
Write-Host ("=" * 74)
Write-Host "  启动文件夹：$startupDir"
Write-Host ""

$installed = Test-Path $shortcutPath
$procs = @(Get-Process -Name 'ALSPD-Agent' -ErrorAction SilentlyContinue)
$pyProcs = @(Get-Process -Name 'python', 'pythonw' -ErrorAction SilentlyContinue |
             Where-Object { $_.Path -like '*alspd-desk*' })

# ---------------- 状态 ----------------
Write-Host "  开机自启：$(if ($installed) { '已启用' } else { '未启用' })"
if ($installed) {
    Write-Host "    快捷方式：$shortcutPath"
    try {
        $shell = New-Object -ComObject WScript.Shell
        $lnk = $shell.CreateShortcut($shortcutPath)
        Write-Host "    目标    ：$($lnk.TargetPath) $($lnk.Arguments)"
    } catch {
        Write-Host "    （读取快捷方式详情失败：$($_.Exception.Message)）"
    }
}
Write-Host "  Agent 进程：$(if ($procs.Count -gt 0) { "$($procs.Count) 个（exe）" } else { '无' })"
if ($pyProcs.Count -gt 0) {
    Write-Host "              另有 $($pyProcs.Count) 个 python/pythonw 进程来自本项目"
}
Write-Host ""

if ($Status) {
    Write-Host ("=" * 74)
    exit 0
}

# ---------------- 删除快捷方式 ----------------
if ($installed) {
    Remove-Item $shortcutPath -Force
    if (Test-Path $shortcutPath) {
        Write-Host "  ❌ 删除失败：$shortcutPath" -ForegroundColor Red
        exit 1
    }
    Write-Host "  ✅ 已取消开机自启（快捷方式已删除）" -ForegroundColor Green
} else {
    Write-Host "  无需操作：本来就没有开机自启"
}

# ---------------- 结束进程 ----------------
if ($KillRunning) {
    $all = @($procs) + @($pyProcs)
    if ($all.Count -eq 0) {
        Write-Host "  没有正在运行的 Agent 进程"
    } else {
        foreach ($p in $all) {
            try {
                Stop-Process -Id $p.Id -Force
                Write-Host "  已结束进程 $($p.Name) (PID $($p.Id))"
            } catch {
                Write-Host "  结束进程 $($p.Id) 失败：$($_.Exception.Message)" -ForegroundColor Yellow
            }
        }
        Write-Host "  ✅ 已结束 Agent 进程" -ForegroundColor Green
    }
} elseif ($procs.Count -gt 0 -or $pyProcs.Count -gt 0) {
    Write-Host ""
    Write-Host "  提示：Agent 进程仍在运行。" -ForegroundColor Yellow
    Write-Host "        想去掉自启并同时结束它，请加 -KillRunning：" -ForegroundColor Yellow
    Write-Host "        .\scripts\remove_autostart.ps1 -KillRunning" -ForegroundColor Yellow
}

Write-Host ""
Write-Host ("=" * 74)
