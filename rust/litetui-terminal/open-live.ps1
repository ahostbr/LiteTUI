param(
    [string]$Python = 'C:/Projects/LiteTUI/.venv/Scripts/python.exe',
    [string]$Prompt = 'Reply with exactly one word: Hello. Do not use tools.'
)
$ErrorActionPreference = 'Stop'
$source = (Resolve-Path "$PSScriptRoot/../..").Path
$manifest = Join-Path $PSScriptRoot 'Cargo.toml'
& cargo build --manifest-path $manifest --locked --offline
if ($LASTEXITCODE -ne 0) { throw "Rust build failed: $LASTEXITCODE" }
$root = 'C:/Projects/.scratch/T0565/claude-live-' + [guid]::NewGuid().ToString('N')
& "$PSScriptRoot/target/debug/litetui-rs.exe" --live --python $Python --source $source --data-root $root --backend claude --model claude-sonnet-5-5 --prompt $Prompt --timeout 180
if ($LASTEXITCODE -ne 0) { throw "Live runtime failed: $LASTEXITCODE (isolated data retained at $root)" }
