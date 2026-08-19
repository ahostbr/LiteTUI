# list_chrome_windows.ps1 — every visible top-level window + its process id
Add-Type @"
using System; using System.Runtime.InteropServices; using System.Text;
public class W {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr data);
  public delegate bool EnumWindowsProc(IntPtr h, IntPtr data);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
}
"@
$cb = [W+EnumWindowsProc]{ param($h,$d)
    $sb = New-Object System.Text.StringBuilder 512
    [void][W]::GetWindowText($h, $sb, 512)
    if ([W]::IsWindowVisible($h) -and $sb.Length -gt 0) {
        $p = [uint32]0
        [void][W]::GetWindowThreadProcessId($h, [ref]$p)
        Write-Output ("{0,-8} {1}" -f $p, $sb.ToString())
    }
    return $true
}
[void][W]::EnumWindows($cb, [IntPtr]::Zero)
