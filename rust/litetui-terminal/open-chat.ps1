param([string]$Python = 'C:/Projects/LiteTUI/.venv/Scripts/python.exe')
$ErrorActionPreference = 'Stop'
$source = (Resolve-Path "$PSScriptRoot/../..").Path
& cargo build --manifest-path "$PSScriptRoot/Cargo.toml" --locked --offline
if ($LASTEXITCODE -ne 0) { throw "Rust build failed: $LASTEXITCODE" }
$root = 'C:/Projects/.scratch/T0565/claude-chat-' + [guid]::NewGuid().ToString('N')
Write-Host "Live conversation data (retained): $root"
& "$PSScriptRoot/target/debug/litetui-rs.exe" --interactive --python $Python --source $source --data-root $root --backend claude --model claude-sonnet-5-5 --timeout 180
if ($LASTEXITCODE -ne 0) { throw "Live runtime failed: $LASTEXITCODE (data retained at $root)" }
