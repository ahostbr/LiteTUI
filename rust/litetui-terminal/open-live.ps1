param(
    [string]$Python = 'C:/Projects/LiteTUI/.venv/Scripts/python.exe',
    [string]$Prompt = 'Reply with exactly one word: Hello. Do not use tools.',
    [ValidateSet('strict', 'interactive', 'autonomous')][string]$ToolProfile
)
$ErrorActionPreference = 'Stop'
$profileArgs = @()
if ($PSBoundParameters.ContainsKey('ToolProfile')) { $profileArgs = @('--tool-profile', $ToolProfile) }
$source = (Resolve-Path "$PSScriptRoot/../..").Path
$manifest = Join-Path $PSScriptRoot 'Cargo.toml'
& cargo build --manifest-path $manifest --locked --offline
if ($LASTEXITCODE -ne 0) { throw "Rust build failed: $LASTEXITCODE" }
$root = 'C:/Projects/.scratch/T0565/claude-live-' + [guid]::NewGuid().ToString('N')
& "$PSScriptRoot/target/debug/litetui-rs.exe" --live --python $Python --source $source --data-root $root --backend claude --model claude-sonnet-5-5 --prompt $Prompt --timeout 180 @profileArgs
if ($LASTEXITCODE -ne 0) { throw "Live runtime failed: $LASTEXITCODE (isolated data retained at $root)" }
