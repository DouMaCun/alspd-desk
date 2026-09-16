# -*- coding: utf-8 -*-
<#
.SYNOPSIS
    把 Agent 与 Viewer 打包成可独立运行的 Windows 程序（PyInstaller）。

.DESCRIPTION
    产出：
        dist\ALSPD-Agent\ALSPD-Agent.exe     受控端（onedir）
        dist\ALSPD-Viewer.exe                控制端（onefile，单文件方便拷回家）

    为什么两者形态不同：
    * **Agent** 装在公司电脑上、开机自启、长期常驻。用 onedir：
      启动快（不必每次解压上万个文件），也不容易招杀软误报。
    * **Viewer** 要拷到家里那台电脑上，用 onefile：就一个文件，双击即用。

.PARAMETER Target
    all / agent / viewer，默认 all。

.PARAMETER OneFile
    Agent 也打成单文件（默认 onedir）。

.PARAMETER Windowed
    Viewer 打成不带控制台窗口的纯 GUI 程序。
    注意：`--selftest` 的输出会随之消失（会写进 exe 同目录的 viewer.log），
    所以**建议先用默认的控制台形态调通，稳定后再用这个开关**。

.PARAMETER Clean
    打包前清理 build/ 与 dist/。

.EXAMPLE
    .\scripts\build_exe.ps1
    .\scripts\build_exe.ps1 -Target viewer
    .\scripts\build_exe.ps1 -Clean -Windowed
#>
param(
    [ValidateSet('all', 'agent', 'viewer')]
    [string]$Target = 'all',
    [switch]$OneFile,
    [switch]$Windowed,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Write-Host "❌ 找不到虚拟环境：$python" -ForegroundColor Red
    Write-Host "   请先执行： python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt"
    exit 1
}

Write-Host ""
Write-Host ("=" * 74)
Write-Host "  ALSPD-DESK  打包 (PyInstaller)"
Write-Host ("=" * 74)

& $python -c "import PyInstaller; print('PyInstaller', PyInstaller.__version__)"
if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ 未安装 PyInstaller：.\.venv\Scripts\pip install pyinstaller" -ForegroundColor Red
    exit 1
}

if ($Clean) {
    Write-Host "清理 build\ 与 dist\ ..."
    Remove-Item -Recurse -Force "$root\build", "$root\dist" -ErrorAction SilentlyContinue
}

# 正在运行的旧程序会占住 exe 文件，导致 PyInstaller 报
# PermissionError: [WinError 5] 拒绝访问。先结束它们，并明确告诉用户。
$running = @(Get-Process -Name 'ALSPD-Agent', 'ALSPD-Viewer' -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    Write-Host ""
    Write-Host "  检测到正在运行的 ALSPD 进程，先结束它们" -ForegroundColor Yellow
    Write-Host "  （旧进程会占住 exe，不结束的话打包会报「拒绝访问」）" -ForegroundColor Yellow
    foreach ($p in $running) {
        Write-Host "    - 结束 $($p.ProcessName) (PID $($p.Id))"
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
}

# 两个入口都依赖 src 下的包；--paths 让 PyInstaller 的静态分析能找到它们
$commonArgs = @(
    '--noconfirm', '--clean', '--log-level', 'WARN',
    '--paths', (Join-Path $root 'src'),
    '--distpath', (Join-Path $root 'dist'),
    '--workpath', (Join-Path $root 'build'),
    '--specpath', (Join-Path $root 'build')
)

# dxcam / mss / win32 等是在函数体内导入的，显式声明更稳
$hidden = @(
    # dxcam 的 numpy 后端是一个编译扩展（_numpy_kernels.**.pyd），
    # 并且是**动态加载**的，PyInstaller 的静态分析发现不了。
    # 不显式收集的话：打包后 dxcam 会静默回落到需要 opencv 的 cv2 后端，
    # 于是采集直接报 "No module named 'cv2'" 而彻底失效。
    # （这个坑是靠 Agent 的 --selftest 抓出来的。）
    '--collect-all', 'dxcam',
    # comtypes 会动态生成模块，同样需要整体收集
    '--collect-all', 'comtypes',
    # websockets 是**动态**导入 python_socks 的（只在用 SOCKS 代理时才 import），
    # PyInstaller 静态分析发现不了 —— 与 dxcam 那次同一类问题。
    '--collect-all', 'python_socks',
    '--hidden-import', 'mss',
    '--hidden-import', 'win32api',
    '--hidden-import', 'win32con',
    '--hidden-import', 'websockets.asyncio.client',
    '--hidden-import', 'websockets.asyncio.server',
    '--exclude-module', 'tkinter',
    '--exclude-module', 'matplotlib',
    '--exclude-module', 'pytest',
    '--exclude-module', 'IPython'
)

$script:agentExe = $null
$script:viewerExe = $null

function Invoke-PyInstaller {
    param([string[]]$PyArgs)
    # 两个 PowerShell 5.1 的坑都在这里：
    #
    # 1) PyInstaller 把 INFO 日志写到 **stderr**。而 PowerShell 5.1 在
    #    $ErrorActionPreference='Stop' 下，会把原生命令写到 stderr 的任何内容
    #    当成终止性错误 —— 结果一且正常也会中断。所以这里临时放宽。
    #
    # 2) 不能用 `2>&1 | Out-Host`：那样会把输出并进函数返回值，
    #    把 $agentExe / $viewerExe 污染成一堆日志文本。这里让输出自然流向控制台。
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $python -m PyInstaller @PyArgs
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($code -ne 0) { throw "PyInstaller 失败（退出码 $code）" }
}

if ($Target -eq 'all' -or $Target -eq 'agent') {
    Write-Host ""
    Write-Host "---- 打包 Agent（受控端）----" -ForegroundColor Cyan
    $mode = if ($OneFile) { '--onefile' } else { '--onedir' }
    Write-Host "  形态: $mode（保留控制台，方便看日志）"
    $pyArgs = @('src\agent\main.py', '--name', 'ALSPD-Agent', $mode, '--console') + $commonArgs + $hidden
    Invoke-PyInstaller -PyArgs $pyArgs
    $script:agentExe = 'dist\ALSPD-Agent\ALSPD-Agent.exe'
    if (-not (Test-Path $script:agentExe)) { $script:agentExe = 'dist\ALSPD-Agent.exe' }
}

if ($Target -eq 'all' -or $Target -eq 'viewer') {
    Write-Host ""
    Write-Host "---- 打包 Viewer（控制端）----" -ForegroundColor Cyan
    $consoleMode = if ($Windowed) { '--windowed' } else { '--console' }
    Write-Host "  形态: --onefile $consoleMode"
    if ($Windowed) {
        Write-Host "  注：--windowed 下 --selftest 的输出会写进 viewer.log" -ForegroundColor Yellow
    }
    $pyArgs = @('src\viewer\main.py', '--name', 'ALSPD-Viewer', '--onefile', $consoleMode) + $commonArgs + $hidden
    Invoke-PyInstaller -PyArgs $pyArgs
    $script:viewerExe = 'dist\ALSPD-Viewer.exe'
}

# 把配置示例放到产物旁边，首次使用直接改名即可
$example = Join-Path $root 'config.example.toml'
foreach ($p in @($script:agentExe, $script:viewerExe)) {
    if ($p -and (Test-Path $p)) {
        Copy-Item $example (Join-Path (Split-Path -Parent (Resolve-Path $p)) 'config.example.toml') -Force
    }
}

Write-Host ""
Write-Host ("=" * 74)
Write-Host "  打包完成" -ForegroundColor Green
Write-Host ("=" * 74)
foreach ($p in @($script:agentExe, $script:viewerExe)) {
    if ($p -and (Test-Path $p)) {
        $item = Get-Item $p
        Write-Host ("  ✅ {0,-24} {1,9:N1} MB" -f $item.Name, ($item.Length / 1MB))
        Write-Host ("     {0}" -f $item.FullName)
    }
}
Write-Host ""
Write-Host "  下一步："
Write-Host "    1) 自检（不联网、不注入键鼠）："
if ($script:agentExe)  { Write-Host "       $($script:agentExe) --selftest" }
if ($script:viewerExe) { Write-Host "       $($script:viewerExe) --selftest" }
Write-Host "    2) 把 config.toml 放到 exe 同目录，然后运行"
Write-Host "    3) 开机自启：.\scripts\install_autostart.ps1"
Write-Host ("=" * 74)
