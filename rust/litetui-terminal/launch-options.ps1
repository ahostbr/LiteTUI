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
