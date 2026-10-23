#requires -Version 5.1
<#
.SYNOPSIS
  Validate projectmem source/configuration, then initialize and run tunnel-client.

.DESCRIPTION
  This script performs the preflight, applies the verified environment to the
  current PowerShell process, runs tunnel-client init, and then tunnel-client run.
  The MCP subprocess inherits PYTHONPATH and UTF-8 settings from tunnel-client.

.EXAMPLE
  .\start_tunnel_client.ps1
  Prompts for the existing tunnel ID.

.EXAMPLE
  .\start_tunnel_client.ps1 -TunnelId 'your-existing-tunnel-id'
#>
[CmdletBinding()]
param(
    [string]$TunnelId,
    [string]$Profile = 'projectmem',
    [string]$HealthListenAddr = '127.0.0.1:8981',
    [string]$Python = 'C:\Python314\python.exe',
    [string]$TunnelClientCommand = 'tunnel-client'
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$PreflightScript = Join-Path $ProjectRoot 'prepare_projectmem.py'

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "找不到 Python 解释器：$Python"
}
if (-not (Test-Path -LiteralPath $PreflightScript -PathType Leaf)) {
    throw "找不到启动前检查程序：$PreflightScript"
}

$TunnelClientInfo = Get-Command $TunnelClientCommand -CommandType Application -ErrorAction Stop
$TunnelClientPath = $TunnelClientInfo.Source

Write-Host '[1/4] 检查 projectmem 源码、注册项目和多项目路由...'
$PreflightOutput = & $Python $PreflightScript --json
$PreflightExitCode = $LASTEXITCODE
if ($PreflightExitCode -ne 0) {
    throw "projectmem 启动前检查失败（退出码 $PreflightExitCode）；未运行 tunnel-client init。"
}

try {
    $PreflightJson = $PreflightOutput -join [Environment]::NewLine
    $Config = $PreflightJson | ConvertFrom-Json
}
catch {
    throw "无法解析 prepare_projectmem.py 的 JSON 输出：$($_.Exception.Message)"
}

$ExpectedRoot = [System.IO.Path]::GetFullPath($ProjectRoot).TrimEnd([char]92)
$ReportedRoot = [System.IO.Path]::GetFullPath([string]$Config.repository_root).TrimEnd([char]92)
if (-not $ReportedRoot.Equals(
    $ExpectedRoot,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "启动前检查返回了意外的项目目录：$ReportedRoot"
}

if (-not $Config.global_mode -or $null -ne $Config.project_root) {
    throw 'projectmem 未处于全局多项目模式；已停止启动。'
}
if (@($Config.projects).Count -lt 2) {
    throw 'projectmem 注册项目少于两个；已停止启动。'
}

Write-Host '[2/4] 应用经过验证的源码路径和 UTF-8 环境...'
$env:PYTHONPATH = [string]$Config.environment.PYTHONPATH
$env:PYTHONUTF8 = [string]$Config.environment.PYTHONUTF8
$env:PYTHONIOENCODING = [string]$Config.environment.PYTHONIOENCODING
Remove-Item Env:PROJECTMEM_ROOT -ErrorAction SilentlyContinue

# 用将要运行 MCP 的同一个解释器验证环境变量确实生效。
$VerifyCode = 'import pathlib, projectmem.mcp_server as server; print(pathlib.Path(server.__file__).resolve()); print(server.GLOBAL_MODE)'
$VerifyOutput = & $Python -c $VerifyCode
$VerifyExitCode = $LASTEXITCODE
if ($VerifyExitCode -ne 0) {
    throw "应用环境后无法导入 projectmem MCP Server（退出码 $VerifyExitCode）。"
}

$ActualModulePath = [System.IO.Path]::GetFullPath([string]$VerifyOutput[0])
$ExpectedSourcePrefix = [System.IO.Path]::GetFullPath(
    (Join-Path $ProjectRoot 'src')
).TrimEnd([char]92) + [char]92
if (-not $ActualModulePath.StartsWith(
    $ExpectedSourcePrefix,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "MCP Server 未从当前仓库源码加载：$ActualModulePath"
}
if ([string]$VerifyOutput[-1] -ne 'True') {
    throw 'MCP Server 未启用全局多项目模式；已停止启动。'
}

Write-Host "  Python 源码：$ActualModulePath"
Write-Host "  已注册项目：$(@($Config.projects).Count)"
Write-Host '  UTF-8 环境：已设置；PROJECTMEM_ROOT：未设置'

if ([string]::IsNullOrWhiteSpace($TunnelId)) {
    $TunnelId = Read-Host '请输入原来使用的 tunnel ID'
}
if ([string]::IsNullOrWhiteSpace($TunnelId)) {
    throw 'Tunnel ID 不能为空；为避免初始化错误的隧道，已停止。'
}

# tunnel-client parses backslashes as escapes in --mcp-command; normalize the executable path.
$McpPython = [System.IO.Path]::GetFullPath($Python).Replace('\', '/')
$McpCommand = '"' + $McpPython + '" -m projectmem.mcp_server'
Write-Host '[3/4] 初始化 tunnel-client profile（保留原来的 --force 行为）...'
$InitArgs = @(
    'init',
    '--sample', 'sample_mcp_stdio_local',
    '--profile', $Profile,
    '--tunnel-id', $TunnelId,
    '--health-listen-addr', $HealthListenAddr,
    '--mcp-command', $McpCommand,
    '--force'
)
& $TunnelClientPath @InitArgs
$InitExitCode = $LASTEXITCODE
if ($InitExitCode -ne 0) {
    throw "tunnel-client init 失败（退出码 $InitExitCode）；未启动 tunnel-client run。"
}

try {
    $Host.UI.RawUI.WindowTitle = 'projectmem'
}
catch {
    # Some non-interactive PowerShell hosts do not expose a writable title.
}
Write-Host '[4/4] 启动 tunnel-client...'
& $TunnelClientPath run --profile $Profile
$RunExitCode = $LASTEXITCODE
if ($RunExitCode -ne 0) {
    throw "tunnel-client run 已退出，退出码：$RunExitCode"
}
