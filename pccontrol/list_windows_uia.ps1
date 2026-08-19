# list_windows_uia.ps1 — all top-level windows, with process + title
param([string]$Proc = 'chrome')
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$desktop = [System.Windows.Automation.AutomationElement]::RootElement
$cond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ProcessIdProperty, 0)
# walk children of desktop
$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
$child = $walker.GetFirstChild($desktop)
while ($null -ne $child) {
    try {
        $pid_ = $child.Current.ProcessId
        $name = $child.Current.Name
        if ($name -and $name.Trim() -ne '') {
            $pname = (Get-Process -Id $pid_ -ErrorAction SilentlyContinue).ProcessName
            Write-Output ("{0,-24} pid={1,-8} {2}" -f $pname, $pid_, $name)
        }
    } catch {}
    $child = $walker.GetNextSibling($child)
}
