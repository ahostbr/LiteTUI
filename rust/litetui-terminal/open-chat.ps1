param(
    [string]$Python = 'C:/Projects/LiteTUI/.venv/Scripts/python.exe',
    [ValidateSet('off', 'on')][string]$Tools = 'off',
    [ValidateSet('strict', 'interactive', 'autonomous')][string]$ToolProfile,
    [string]$Workspace
)
$ErrorActionPreference = 'Stop'
if ($Tools -eq 'on' -and [string]::IsNullOrWhiteSpace($Workspace)) {
    throw 'Tools on requires -Workspace naming the directory to work in'
}
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
$root = 'C:/Projects/.scratch/T0565/claude-chat-' + [guid]::NewGuid().ToString('N')
Write-Host "Live conversation data (retained): $root"
& "$PSScriptRoot/target/debug/litetui-rs.exe" --interactive --python $Python --source $source --data-root $root --backend claude --model claude-sonnet-5-5 --timeout 180 --tools $Tools @profileArgs @workspaceArgs
if ($LASTEXITCODE -ne 0) { throw "Live runtime failed: $LASTEXITCODE (data retained at $root)" }
