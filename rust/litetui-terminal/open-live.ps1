param(
    [string]$Python,
    [string]$Prompt = 'Reply with exactly one word: Hello. Do not use tools.',
    [ValidateSet('strict', 'interactive', 'autonomous')][string]$ToolProfile
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'launch-options.ps1')
$profileArgs = @()
if ($PSBoundParameters.ContainsKey('ToolProfile')) { $profileArgs = @('--tool-profile', $ToolProfile) }
$source = (Resolve-Path "$PSScriptRoot/../..").Path
if ([string]::IsNullOrWhiteSpace($Python)) { $Python = Resolve-LiteTuiPython $source }
$manifest = Join-Path $PSScriptRoot 'Cargo.toml'
& cargo build --manifest-path $manifest --locked --offline
if ($LASTEXITCODE -ne 0) { throw "Rust build failed: $LASTEXITCODE" }
$scratch = if ($env:LITETUI_SCRATCH) { $env:LITETUI_SCRATCH } else { Join-Path ([System.IO.Path]::GetTempPath()) 'litetui-rust' }
$root = Join-Path $scratch ('claude-live-' + [guid]::NewGuid().ToString('N'))
& "$PSScriptRoot/target/debug/litetui-rs.exe" --live --python $Python --source $source --data-root $root --backend claude --model claude-sonnet-5-5 --prompt $Prompt --timeout 180 @profileArgs
if ($LASTEXITCODE -ne 0) { throw "Live runtime failed: $LASTEXITCODE (isolated data retained at $root)" }
