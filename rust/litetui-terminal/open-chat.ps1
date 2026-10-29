param(
    [string]$Python = 'C:/Projects/LiteTUI/.venv/Scripts/python.exe',
    [ValidateSet('off', 'on')][string]$Tools = 'off',
    [ValidateSet('strict', 'interactive', 'autonomous')][string]$ToolProfile,
    [string]$Workspace,
    [string]$DataRoot,
    [string]$Resume,
    [ValidateSet('claude', 'codex')][string]$Backend = 'claude',
    [string]$Model = 'claude-sonnet-5-5'
)
$ErrorActionPreference = 'Stop'
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
& cargo build --manifest-path "$PSScriptRoot/Cargo.toml" --locked --offline
if ($LASTEXITCODE -ne 0) { throw "Rust build failed: $LASTEXITCODE" }
$root = if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    'C:/Projects/.scratch/T0565/claude-chat-' + [guid]::NewGuid().ToString('N')
} else {
    [System.IO.Path]::GetFullPath($DataRoot)
}
Write-Host "Live conversation data (retained): $root"
& "$PSScriptRoot/target/debug/litetui-rs.exe" --interactive --python $Python --source $source --data-root $root --backend $Backend --model $Model --timeout 180 --tools $Tools @profileArgs @workspaceArgs @resumeArgs
if ($LASTEXITCODE -ne 0) { throw "Live runtime failed: $LASTEXITCODE (data retained at $root)" }
