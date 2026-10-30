#Requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Destination,
    [Parameter(Mandatory)][string]$Executable
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Stage a supplied build; no build, installation, dependency download or runtime launch.
# Its hash is recorded. This script does not claim the binary was built from this snapshot.
$source = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$binary = (Get-Item -LiteralPath $Executable -Force)
if ($binary.PSIsContainer -or ($binary.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw 'Executable must be a regular file'
}
$destinationPath = [IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $destinationPath) { throw 'Destination already exists; choose a new package directory' }

function Invoke-GitText([string[]]$Arguments) {
    $result = & git -C $source @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Git inspection failed: $LASTEXITCODE" }
    return ($result -join [Environment]::NewLine)
}
function Assert-RegularSource([string]$Relative) {
    if ([IO.Path]::IsPathRooted($Relative) -or $Relative -match '(^|[/\\])\.\.([/\\]|$)|[:\x00-\x1f]') {
        throw "Invalid tracked resource path: $Relative"
    }
    $current = $source
    foreach ($part in ($Relative -split '[/\\]')) {
        $current = Join-Path $current $part
        $item = Get-Item -LiteralPath $current -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Linked runtime resource cannot be packaged: $Relative"
        }
    }
    if ($item.PSIsContainer) { throw "Expected tracked runtime file: $Relative" }
    return $current
}
$revision = (Invoke-GitText @('rev-parse', 'HEAD')).Trim()
if ($revision -notmatch '^[a-f0-9]{40}$') { throw 'Cannot identify source revision' }
$paths = @('src/litetui', 'tools/chrome-bridge', 'tools/pccontrol', 'tools/convo_search.py',
           'prompts', 'pyproject.toml', 'uv.lock', 'README.md', 'LICENSE', 'LICENSE.txt',
           'rust/litetui-terminal/package-launch.ps1')
$tracked = (Invoke-GitText (@('ls-files', '-z', '--') + $paths)).Split([char]0, [StringSplitOptions]::RemoveEmptyEntries)
$required = @('src/litetui/cli.py', 'src/litetui/app.py', 'src/litetui/schemas/__init__.py',
              'src/litetui/prompts/systemprompt.md', 'tools/chrome-bridge/bridge.py',
              'tools/pccontrol/pccontrol.py', 'tools/pccontrol/screenshot.ps1',
              'tools/convo_search.py', 'pyproject.toml', 'uv.lock',
              'rust/litetui-terminal/package-launch.ps1')
foreach ($path in $required) {
    if ($tracked -cnotcontains $path) { throw "Required runtime file is not tracked: $path" }
}
if ($tracked.Count -gt 10000) { throw 'Runtime file inventory exceeds the package limit' }
$dirty = -not [string]::IsNullOrWhiteSpace(
    (Invoke-GitText (@('status', '--porcelain=v1', '--untracked-files=no', '--') + $paths)))
$resources = @()
foreach ($relative in ($tracked | Sort-Object -CaseSensitive)) {
    $resource = Assert-RegularSource $relative
    $output = if ($relative -ceq 'rust/litetui-terminal/package-launch.ps1') {
        'launch.ps1'
    } else { 'runtime/' + $relative }
    $resources += [pscustomobject]@{ Source=$resource; Path=$output; Hash=(Get-FileHash -LiteralPath $resource -Algorithm SHA256).Hash.ToLowerInvariant() }
}
$resources += [pscustomobject]@{ Source=$binary.FullName; Path='litetui-rs.exe'; Hash=(Get-FileHash -LiteralPath $binary.FullName -Algorithm SHA256).Hash.ToLowerInvariant() }
# Once staging begins, failure leaves an explicitly incomplete directory. Never delete
# or overwrite a destination which may contain another build or the user's data.
[void][IO.Directory]::CreateDirectory($destinationPath)
$entries = @()
$total = 0L
foreach ($resource in $resources) {
    $output = Join-Path $destinationPath $resource.Path
    [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($output))
    [IO.File]::Copy($resource.Source, $output, $false)
    $file = Get-Item -LiteralPath $output
    $hash = (Get-FileHash -LiteralPath $output -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -cne $resource.Hash) { throw "Resource changed during staging: $($resource.Path)" }
    $total += $file.Length
    if ($file.Length -gt 134217728 -or $total -gt 536870912) { throw 'Package payload exceeds the size limit' }
    $entries += [ordered]@{ path=$resource.Path; bytes=$file.Length; sha256=$hash }
}
foreach ($resource in $resources) {
    if ((Get-FileHash -LiteralPath $resource.Source -Algorithm SHA256).Hash.ToLowerInvariant() -cne $resource.Hash) {
        throw "Source changed during staging: $($resource.Path)"
    }
}
if ((Invoke-GitText @('rev-parse', 'HEAD')).Trim() -cne $revision) { throw 'Source revision changed during staging' }
$dirtyAfter = -not [string]::IsNullOrWhiteSpace(
    (Invoke-GitText (@('status', '--porcelain=v1', '--untracked-files=no', '--') + $paths)))
$manifest = [ordered]@{
    format='litetui-rust-package'; version=1
    created_utc=[DateTime]::UtcNow.ToString('o')
    source=[ordered]@{ base_revision=$revision; working_tree_changes=($dirty -or $dirtyAfter); snapshot='file-hashes' }
    executable=[ordered]@{ path='litetui-rs.exe'; provenance='supplied-build; source correspondence not verified' }
    python=[ordered]@{ bundled=$false; minimum='3.11'; dependencies='runtime/pyproject.toml'; lock='runtime/uv.lock' }
    files=$entries
}
# The manifest is the completion marker and is published last.
$manifestPath = Join-Path $destinationPath 'package-manifest.json'
[IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 8) + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
& (Join-Path $destinationPath 'launch.ps1') -VerifyOnly
Write-Host "Staged package: $destinationPath"
Write-Host 'Python/dependencies remain external. No application runtime was started.'
