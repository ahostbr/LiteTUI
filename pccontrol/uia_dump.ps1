# uia_dump.ps1 — dump visible text from a window's UI Automation tree (depth-limited)
param(
    [string]$Title = 'chrome',   # process name or title substring
    [int]$Depth = 25,
    [int]$MaxLines = 120
)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$proc = Get-Process | Where-Object { $_.MainWindowTitle -ne '' -and (
        ($_.ProcessName -like "*$Title*") -or ($_.MainWindowTitle -like "*$Title*")) } | Select-Object -First 1
if (-not $proc) { Write-Output "ERROR: no window matching '$Title'"; exit 1 }

$root = [System.Windows.Automation.AutomationElement]::FromHandle($proc.MainWindowHandle)
$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
$count = 0

function Walk($el, $d) {
    if ($null -eq $el -or $d -gt $script:Depth) { return }
    $name = $el.Current.Name
    if ($name -and $name.Trim() -ne '' -and $el.Current.IsOffscreen -eq $false) {
        Write-Output ('  ' * $d + $name.Trim())
        $script:count++
    }
    $child = $walker.GetFirstChild($el)
    while ($null -ne $child) {
        Walk $child ($d + 1)
        if ($script:count -ge $MaxLines) { return }
        $child = $walker.GetNextSibling($child)
    }
}

Write-Output ("WINDOW: " + $proc.ProcessName + " | " + $proc.MainWindowTitle)
Walk $root 0
Write-Output ("... ({} lines shown)".format([Math]::Min($count, $MaxLines)))
