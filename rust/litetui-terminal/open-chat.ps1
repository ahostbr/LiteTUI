param(
    [string]$Python,
    [ValidateSet('off', 'on')][string]$Tools = 'off',
    [ValidateSet('strict', 'interactive', 'autonomous')][string]$ToolProfile,
    [string]$Workspace,
    [string]$DataRoot,
    [string]$Resume,
    [string]$Owner,
    [string]$Memory,
    [string]$Backend = 'claude',
    [string]$Model = 'claude-sonnet-5-5',
    [string]$BaseUrl,
    [ValidateSet('auto', 'connect', 'start')][string]$ServerMode,
    [switch]$StartServer,
    [switch]$LoadModel,
    [switch]$EngineManagement,
    [UInt64]$ContextLength,
    [UInt64]$MaxTokens,
    [string]$ServerExecutable,
    [string]$ModelPath,
    [string]$ServerCommand,
    [string]$ApiKeyEnv,
    [UInt64]$ServerTimeout,
    [ValidateSet('native', 'http')][string]$CodexEngine,
    [string]$ThinkingLevel
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'launch-options.ps1')
$backendArgs = @(Get-LiteTuiLaunchArguments $PSBoundParameters)
if ($Backend -ne 'claude' -and -not $PSBoundParameters.ContainsKey('Model')) {
    throw 'Selecting another backend requires an explicit -Model'
}
if ($Tools -eq 'on' -and [string]::IsNullOrWhiteSpace($Workspace)) {
    throw 'Tools on requires -Workspace naming the directory to work in'
}
$resumeArgs = @()
if (-not [string]::IsNullOrWhiteSpace($Resume)) {
    if ([string]::IsNullOrWhiteSpace($DataRoot)) { throw 'Resume requires -DataRoot naming the retained Rust conversation root' }
    $resumeArgs = @('--resume', $Resume)
}
if ([string]::IsNullOrWhiteSpace($Model)) { throw 'Model must name the saved or newly selected model' }
$profileArgs = @()
if ($PSBoundParameters.ContainsKey('ToolProfile')) { $profileArgs = @('--tool-profile', $ToolProfile) }
$workspaceArgs = @()
if (-not [string]::IsNullOrWhiteSpace($Workspace)) {
    $resolvedWorkspace = (Resolve-Path -LiteralPath $Workspace).Path
    if (-not (Test-Path -LiteralPath $resolvedWorkspace -PathType Container)) { throw 'Workspace must be a directory' }
    $workspaceArgs = @('--workspace', $resolvedWorkspace)
}
$source = (Resolve-Path "$PSScriptRoot/../..").Path
if ([string]::IsNullOrWhiteSpace($Python)) { $Python = Resolve-LiteTuiPython $source }
Write-Host "Python backend: $Python"
& cargo build --manifest-path "$PSScriptRoot/Cargo.toml" --locked --offline
if ($LASTEXITCODE -ne 0) { throw "Rust build failed: $LASTEXITCODE" }
$scratch = if ($env:LITETUI_SCRATCH) { $env:LITETUI_SCRATCH } else { Join-Path ([System.IO.Path]::GetTempPath()) 'litetui-rust' }
$root = if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    Join-Path $scratch ('claude-chat-' + [guid]::NewGuid().ToString('N'))
} else {
    [System.IO.Path]::GetFullPath($DataRoot)
}
Write-Host "Live conversation data (retained): $root"
& "$PSScriptRoot/target/debug/litetui-rs.exe" --interactive --python $Python --source $source --data-root $root --backend $Backend --model $Model --timeout 180 --tools $Tools @profileArgs @workspaceArgs @resumeArgs @backendArgs
if ($LASTEXITCODE -ne 0) { throw "Live runtime failed: $LASTEXITCODE (data retained at $root)" }
