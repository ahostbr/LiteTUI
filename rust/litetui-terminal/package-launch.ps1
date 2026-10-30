#Requires -Version 7.0
[CmdletBinding(DefaultParameterSetName='Run')]
param(
    [Parameter(Mandatory, ParameterSetName='Verify')][switch]$VerifyOnly,
    [Parameter(Mandatory, ParameterSetName='Run')][string]$Python,
    [Parameter(Mandatory, ParameterSetName='Run')][string]$Backend,
    [Parameter(Mandatory, ParameterSetName='Run')][string]$Model,
    [Parameter(ParameterSetName='Run')][ValidateSet('off', 'on')][string]$Tools = 'off',
    [Parameter(ParameterSetName='Run')][ValidateSet('strict', 'interactive', 'autonomous')][string]$ToolProfile,
    [Parameter(ParameterSetName='Run')][string]$Workspace,
    [Parameter(ParameterSetName='Run')][string]$DataRoot,
    [Parameter(ParameterSetName='Run')][string]$Resume,
    [Parameter(ParameterSetName='Run')][ValidateRange(1,600)][int]$Timeout = 180,
    [Parameter(ParameterSetName='Run')][string]$BaseUrl,
    [Parameter(ParameterSetName='Run')][ValidateSet('auto', 'connect', 'start')][string]$ServerMode,
    [Parameter(ParameterSetName='Run')][switch]$StartServer,
    [Parameter(ParameterSetName='Run')][switch]$LoadModel,
    [Parameter(ParameterSetName='Run')][switch]$EngineManagement,
    [Parameter(ParameterSetName='Run')][UInt64]$ContextLength,
    [Parameter(ParameterSetName='Run')][UInt64]$MaxTokens,
    [Parameter(ParameterSetName='Run')][string]$ServerExecutable,
    [Parameter(ParameterSetName='Run')][string]$ModelPath,
    [Parameter(ParameterSetName='Run')][string]$ServerCommand,
    [Parameter(ParameterSetName='Run')][string]$ApiKeyEnv,
    [Parameter(ParameterSetName='Run')][UInt64]$ServerTimeout,
    [Parameter(ParameterSetName='Run')][ValidateSet('native', 'http')][string]$CodexEngine,
    [Parameter(ParameterSetName='Run')][string]$ThinkingLevel
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$package = [IO.Path]::GetFullPath($PSScriptRoot)
$prefix = $package.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar

function Resolve-PackageFile([string]$Relative) {
    if ([string]::IsNullOrWhiteSpace($Relative) -or [IO.Path]::IsPathRooted($Relative) -or
        $Relative -match '\\|[:\x00-\x1f]|(^|/)\.{1,2}(/|$)|//|/$') {
        throw "Invalid package path: $Relative"
    }
    $resolved = [IO.Path]::GetFullPath((Join-Path $package $Relative))
    if (-not $resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Package path escapes its root: $Relative"
    }
    $current = $package
    foreach ($part in ($Relative -split '/')) {
        $current = Join-Path $current $part
        [void](Assert-Unlinked $current)
    }
    return $resolved
}
function Assert-Unlinked([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Linked package path: $Path" }
    return $item
}
function Assert-UnlinkedDirectoryAncestry([string]$Path, [string]$Role) {
    # Check both sides of containment: an external data root or a package parent
    # can otherwise alias the same directory through a junction.
    $current = [IO.Path]::GetFullPath($Path)
    while (-not [string]::IsNullOrEmpty($current)) {
        $item = $null
        try { $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop }
        catch [System.Management.Automation.ItemNotFoundException] { }
        if ($null -ne $item) {
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Linked $Role path: $current"
            }
            if (-not $item.PSIsContainer) { throw "$Role ancestor must be a directory: $current" }
        }
        $current = [IO.Path]::GetDirectoryName($current)
    }
}
Assert-UnlinkedDirectoryAncestry $package 'package'
[void](Assert-Unlinked $package)
$manifestPath = Join-Path $package 'package-manifest.json'
$manifestFile = Assert-Unlinked $manifestPath
if ($manifestFile.PSIsContainer -or $manifestFile.Length -gt 8388608) { throw 'Invalid package manifest size' }
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.format -cne 'litetui-rust-package' -or
    ($manifest.version -isnot [long] -and $manifest.version -isnot [int]) -or $manifest.version -ne 1 -or
    $manifest.source.base_revision -cnotmatch '^[a-f0-9]{40}$' -or
    $manifest.source.working_tree_changes -isnot [bool] -or
    $manifest.executable.path -cne 'litetui-rs.exe' -or $manifest.python.bundled -isnot [bool] -or $manifest.python.bundled) {
    throw 'Unsupported or malformed package manifest'
}
$files = @($manifest.files)
if ($files.Count -lt 5 -or $files.Count -gt 10001) { throw 'Invalid package file count' }
$expected = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
$total = 0L
foreach ($entry in $files) {
    if ($entry.path -isnot [string] -or -not $expected.Add($entry.path) -or
        $entry.path -ieq 'package-manifest.json' -or
        $entry.sha256 -isnot [string] -or $entry.sha256 -cnotmatch '^[a-f0-9]{64}$' -or
        ($entry.bytes -isnot [long] -and $entry.bytes -isnot [int]) -or
        $entry.bytes -lt 0 -or $entry.bytes -gt 134217728) {
        throw 'Invalid or duplicate package file record'
    }
    $path = Resolve-PackageFile $entry.path
    $file = Assert-Unlinked $path
    if ($file.PSIsContainer -or $file.Length -ne $entry.bytes -or
        (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $entry.sha256) {
        throw "Package file failed verification: $($entry.path)"
    }
    $total += $file.Length
    if ($total -gt 536870912) { throw 'Package exceeds the size limit' }
}
# Walk directories explicitly so a junction is rejected before traversing it.
$directories = [Collections.Generic.Stack[string]]::new()
$directories.Push($package)
while ($directories.Count -gt 0) {
    $directory = $directories.Pop()
    [void](Assert-Unlinked $directory)
    foreach ($item in Get-ChildItem -LiteralPath $directory -Force) {
        [void](Assert-Unlinked $item.FullName)
        if ($item.PSIsContainer) { $directories.Push($item.FullName); continue }
        $relative = [IO.Path]::GetRelativePath($package, $item.FullName).Replace('\', '/')
        if ($relative -cne 'package-manifest.json' -and -not $expected.Contains($relative)) {
            throw "Unlisted package file: $relative"
        }
    }
}
foreach ($required in @('launch.ps1', 'launch-options.ps1', 'litetui-rs.exe', 'runtime/src/litetui/cli.py',
                        'runtime/src/litetui/app.py', 'runtime/pyproject.toml', 'runtime/uv.lock')) {
    if (-not $expected.Contains($required)) { throw "Missing package requirement: $required" }
}
Write-Host "Verified $($files.Count) package files; source $($manifest.source.base_revision); working tree changes: $($manifest.source.working_tree_changes)"
if ($VerifyOnly) { return }
. (Join-Path $package 'launch-options.ps1')
$backendArgs = @(Get-LiteTuiLaunchArguments $PSBoundParameters)

if ([string]::IsNullOrWhiteSpace($Model)) { throw 'Model must be selected explicitly' }
$pythonFile = Get-Item -LiteralPath $Python -Force
if ($pythonFile.PSIsContainer) { throw 'Python must name an executable file in the intended environment' }
$workspaceArgs = @()
if (-not [string]::IsNullOrWhiteSpace($Workspace)) {
    $workspacePath = (Resolve-Path -LiteralPath $Workspace).Path
    if (-not (Test-Path -LiteralPath $workspacePath -PathType Container)) { throw 'Workspace must be a directory' }
    $workspaceArgs = @('--workspace', $workspacePath)
}
if ($Tools -eq 'on' -and $workspaceArgs.Count -eq 0) { throw 'Tools on requires an explicit -Workspace' }
$resumeArgs = @()
if (-not [string]::IsNullOrWhiteSpace($Resume)) {
    if ([string]::IsNullOrWhiteSpace($DataRoot)) { throw 'Resume requires the retained -DataRoot' }
    $resumeArgs = @('--resume', $Resume)
}
$root = if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    $local = [Environment]::GetFolderPath('LocalApplicationData')
    if ([string]::IsNullOrWhiteSpace($local)) { throw 'Local application data is unavailable; pass -DataRoot' }
    Join-Path $local ('LiteTUI/Rust/conversations/' + [guid]::NewGuid().ToString('N'))
} else { [IO.Path]::GetFullPath($DataRoot) }
if ($root -ieq $package -or $root.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'DataRoot must be outside the immutable package directory'
}
Assert-UnlinkedDirectoryAncestry $root 'DataRoot'
$profileArgs = @()
if ($PSBoundParameters.ContainsKey('ToolProfile')) { $profileArgs = @('--tool-profile', $ToolProfile) }
$source = Join-Path $package 'runtime'
$executable = Join-Path $package 'litetui-rs.exe'
$launchArgs = @('--interactive', '--python', $pythonFile.FullName, '--source', $source,
    '--data-root', $root, '--backend', $Backend, '--model', $Model, '--timeout', [string]$Timeout,
    '--tools', $Tools) + $profileArgs + $workspaceArgs + $resumeArgs + $backendArgs
Write-Host "Live conversation data (retained): $root"
$previousBytecode = $env:PYTHONDONTWRITEBYTECODE
try {
    # Preserve the verified source tree across interpreter imports and child tools.
    $env:PYTHONDONTWRITEBYTECODE = '1'
    & $executable @launchArgs
} finally {
    $env:PYTHONDONTWRITEBYTECODE = $previousBytecode
}
if ($LASTEXITCODE -ne 0) { throw "Live runtime failed: $LASTEXITCODE (data retained at $root)" }
