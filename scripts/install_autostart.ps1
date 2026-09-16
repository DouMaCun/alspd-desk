# -*- coding: utf-8 -*-
<#
.SYNOPSIS
    让 Agent（受控端）开机自动启动。

.DESCRIPTION
    在公司电脑登录后自动运行 Agent，这样你从家里随时都能连上，不必每天早上手动开。

    实现方式：在**当前用户的启动文件夹**里放一个快捷方式。
    —— 不需要管理员权限，不改注册表，不装服务。删掉快捷方式即可撤销。

    ⚠️ 安全提示
    ------------------
    开机自启意味着 Agent 会**长期常驻并持续抓取屏幕**。请确认：
      * 这是你确实想要的行为
      * config.toml 里的 allow_input 是否符合你的预期
        （false = 只能看不能操作，更安全）
      * 急停热键你记得住（默认 Ctrl+Alt+Shift+Q）

.PARAMETER ExePath
    Agent 可执行文件路径。默认自动找 dist\ALSPD-Agent\ALSPD-Agent.exe，
    找不到再退回到源码方式（pythonw 直接跑 src\agent\main.py）。

.PARAMETER StartupArgs
    传给 Agent 的附加参数，例如 "-readonly"。

.EXAMPLE
    .\scripts\install_autostart.ps1
    .\scripts\install_autostart.ps1 -StartupArgs "-readonly"
#>
param(
    [string]$ExePath,
    [string]$StartupArgs = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$shortcutName = 'ALSPD-Agent.lnk'
$startupDir = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupDir $shortcutName

Write-Host ""
Write-Host ("=" * 74)
Write-Host "  ALSPD-DESK  Agent 开机自启设置"
Write-Host ("=" * 74)
Write-Host "  启动文件夹：$startupDir"
Write-Host ""

# ---------------- 确定要启动什么 ----------------
$target = $null
$arguments = ''

if (-not $ExePath) {
    $candidates = @(
        (Join-Path $root 'dist\ALSPD-Agent\ALSPD-Agent.exe'),
        (Join-Path $root 'dist\ALSPD-Agent.exe')
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $ExePath = $c; break }
    }
}

if ($ExePath -and (Test-Path $ExePath)) {
    $target = (Resolve-Path $ExePath).Path
    Write-Host "  目标：$target" -ForegroundColor Green
} else {
    # 没有打包产物就退回源码方式
    $pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
    $script = Join-Path $root 'src\agent\main.py'
    if (-not (Test-Path $pythonw)) {
        Write-Host "❌ 既没有打包好的 exe，也找不到 $pythonw" -ForegroundColor Red
        Write-Host "   请先运行：.\scripts\build_exe.ps1"
        exit 1
    }
    if (-not (Test-Path $script)) {
        Write-Host "❌ 找不到 $script" -ForegroundColor Red
        exit 1
    }
    $target = $pythonw
    $arguments = '"{0}"' -f $script
    Write-Host "  未找到打包产物，改为源码方式启动（pythonw）" -ForegroundColor Yellow
    Write-Host "  目标：$target $arguments"
}

# ---------------- 检查配置 ----------------
$configPath = $null
$searchDirs = @()
if ($ExePath) { $searchDirs += (Split-Path -Parent $target) }
$searchDirs += $root
foreach ($d in $searchDirs) {
    $c = Join-Path $d 'config.toml'
    if (Test-Path $c) { $configPath = $c; break }
}

if (-not $configPath) {
    Write-Host ""
    Write-Host "  ⚠️  没有找到 config.toml —— Agent 启动后会因为缺少中继地址而反复失败。" -ForegroundColor Yellow
    Write-Host "      请先复制 config.example.toml 为 config.toml 并填写。" -ForegroundColor Yellow
    Write-Host "      查找过：$($searchDirs -join ' , ')"
} else {
    Write-Host "  配置：$configPath"
    $text = Get-Content $configPath -Raw -Encoding UTF8
    # 只做粗略提示，不解析 TOML
    if ($text -match '(?m)^\s*allow_input\s*=\s*true') {
        Write-Host ""
        Write-Host "  ⚠️⚠️  警告：配置里 allow_input = true" -ForegroundColor Red
        Write-Host "        也就是说开机后，任何拿到你密码的人都能**操作这台电脑的键鼠**。" -ForegroundColor Red
        Write-Host "        如果只是自己临时用，建议改成 false（只读观看），需要时再手动开。" -ForegroundColor Red
    } else {
        Write-Host "  模式：只读（allow_input = false）—— 只上传画面，不注入键鼠" -ForegroundColor Green
    }
    $hotkey = [regex]::Match($text, '(?m)^\s*panic_hotkey\s*=\s*"([^"]+)"')
    if ($hotkey.Success) {
        Write-Host "  急停热键：$($hotkey.Groups[1].Value)  ← 出问题时按它" -ForegroundColor Cyan
    }
}

# ---------------- 创建快捷方式 ----------------
if (Test-Path $shortcutPath) {
    Write-Host ""
    Write-Host "  已存在同名快捷方式，将覆盖：$shortcutPath" -ForegroundColor Yellow
}

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($shortcutPath)
$lnk.TargetPath = $target
$lnk.WorkingDirectory = Split-Path -Parent $target
if ($StartupArgs) {
    $arguments = ("{0} {1}" -f $arguments, $StartupArgs).Trim()
}
if ($arguments) { $lnk.Arguments = $arguments }
$lnk.Description = 'ALSPD-DESK 受控端（开机自启）'
$lnk.WindowStyle = 7      # 7 = 最小化启动，不弹出来打扰你
$lnk.Save()

Write-Host ""
if (Test-Path $shortcutPath) {
    Write-Host "  ✅ 已创建开机自启：$shortcutPath" -ForegroundColor Green
    Write-Host ""
    Write-Host "  说明："
    Write-Host "    * 下次登录 Windows 时会自动启动 Agent（窗口最小化）"
    Write-Host "    * 想立刻试一次：双击上面那个快捷方式"
    Write-Host "    * 想取消：运行 .\scripts\remove_autostart.ps1"
} else {
    Write-Host "  ❌ 快捷方式创建失败" -ForegroundColor Red
    exit 1
}
Write-Host ("=" * 74)
