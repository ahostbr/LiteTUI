# The Python that runs LiteTUI's backend: $env:LITETUI_PYTHON; else the main checkout's .venv
# (git's common dir, so a worktree finds the repository's environment with its optional extras such
# as the Claude SDK); else this tree's own .venv; else python on PATH.
function Resolve-LiteTuiPython {
    param([string]$Source)
    if ($env:LITETUI_PYTHON) { return $env:LITETUI_PYTHON }
    # The checkout's own venv first: a worktree synced from its own lock (the port's newer
    # claude-agent-sdk, 2026-11-13) must not run on the main checkout's, which Python
    # LiteTUI's run.bat re-syncs from develop. In the main checkout both are the same.
    $candidates = @(Join-Path $Source '.venv/Scripts/python.exe')
    $common = & git -C $Source rev-parse --path-format=absolute --git-common-dir 2>$null
    if ($LASTEXITCODE -eq 0 -and $common) {
        $candidates += Join-Path (Split-Path $common -Parent) '.venv/Scripts/python.exe'
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return (Resolve-Path -LiteralPath $candidate).Path }
    }
    return (Get-Command python -ErrorAction Stop).Source
}

# Python LiteTUI's data root: $env:LITETUI_DATA_ROOT, else the main checkout (paths.ROOT).
function Resolve-LiteTuiPythonData {
    param([string]$Source)
    if ($env:LITETUI_DATA_ROOT) { return [System.IO.Path]::GetFullPath($env:LITETUI_DATA_ROOT) }
    $common = & git -C $Source rev-parse --path-format=absolute --git-common-dir 2>$null
    if ($LASTEXITCODE -eq 0 -and $common) { return (Split-Path $common -Parent) }
    return $Source
}

# The Rust frontend's one persistent data root, beside Python's and marked as Rust (Ryan
# 2026-11-12: "copy all settings keep them separate marked as rust").
function Resolve-LiteTuiRustData {
    param([string]$PythonData)
    $parent = Split-Path $PythonData -Parent
    return Join-Path $parent ((Split-Path $PythonData -Leaf) + '-rust-data')
}

# Invocation-only forwarding shared by checkout and portable launchers.
# Rust validates combinations before runtime state is created; Python owns readiness.
function Get-LiteTuiLaunchArguments {
    param([System.Collections.IDictionary]$Parameters)
    if ($Parameters['StartServer'] -and ($Parameters.Keys -contains 'ServerMode')) {
        throw 'Choose -StartServer or -ServerMode, not both'
    }
    $result = [System.Collections.Generic.List[string]]::new()
    $fields = [ordered]@{
        Owner='--owner'; Memory='--memory'
        BaseUrl='--base-url'; ServerMode='--server-mode'; ContextLength='--context-length'
        MaxTokens='--max-tokens'; ServerExecutable='--server-executable'; ModelPath='--model-path'
        ServerCommand='--server-command'; ApiKeyEnv='--api-key-env'; ServerTimeout='--server-timeout'
        CodexEngine='--codex-engine'; ThinkingLevel='--thinking-level'
    }
    foreach ($name in $fields.Keys) {
        if (($Parameters.Keys -contains $name)) {
            $result.Add($fields[$name])
            $result.Add([string]$Parameters[$name])
        }
    }
    if ($Parameters['StartServer']) { $result.Add('--start-server') }
    if ($Parameters['LoadModel']) { $result.Add('--load-model') }
    if ($Parameters['EngineManagement']) { $result.Add('--engine-management') }
    return $result.ToArray()
}
