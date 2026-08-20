# marker_overlay.ps1 — Sentinel click-target overlay.
# Draws a topmost, click-THROUGH red ring + crosshair at a screen point, then
# auto-closes after -Ms. Click-through (WS_EX_TRANSPARENT) so it never eats the
# click; NOACTIVATE so it never steals focus. Coords are monitor-LOCAL when -Mon
# >= 0 (same Screen.AllScreens index as screenshot.ps1), else global.
param(
    [int]$Mon = -1,
    [int]$X = 0,
    [int]$Y = 0,
    [int]$Ms = 2500,
    [string]$Label = '',
    [int]$Size = 110,
    [string]$Color = 'red'
)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System; using System.Runtime.InteropServices;
public class NW {
  [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr h, int i);
  [DllImport("user32.dll")] public static extern int SetWindowLong(IntPtr h, int i, int v);
}
"@

$screens = [System.Windows.Forms.Screen]::AllScreens
$gx = $X; $gy = $Y
if ($Mon -ge 0 -and $Mon -lt $screens.Count) {
    $b = $screens[$Mon].Bounds
    $gx = $b.X + $X; $gy = $b.Y + $Y
}

function Resolve-MarkerColor([string]$c) {
    if ($c -match '^#?[0-9A-Fa-f]{6}$') {
        $h = $c.TrimStart('#')
        return [System.Drawing.Color]::FromArgb(255,
            [Convert]::ToInt32($h.Substring(0, 2), 16),
            [Convert]::ToInt32($h.Substring(2, 2), 16),
            [Convert]::ToInt32($h.Substring(4, 2), 16))
    }
    $named = [System.Drawing.Color]::FromName($c)
    if (-not $named.IsKnownColor) {
        return [System.Drawing.Color]::FromArgb(255, 255, 45, 45)   # fallback red
    }
    return [System.Drawing.Color]::FromArgb(255, $named.R, $named.G, $named.B)
}

$size = if ($Size -ge 20) { $Size } else { 110 }
$labelH = if ($Label -ne '') { 20 } else { 0 }
$key = [System.Drawing.Color]::FromArgb(255, 0, 254)   # near-magenta = transparency key (won't collide with picks)
$mc = Resolve-MarkerColor $Color

$f = New-Object System.Windows.Forms.Form
$f.FormBorderStyle = 'None'
$f.StartPosition = 'Manual'
$f.TopMost = $true
$f.ShowInTaskbar = $false
$f.BackColor = $key
$f.TransparencyKey = $key
$f.Size = New-Object System.Drawing.Size($size, ($size + $labelH))
$f.Location = New-Object System.Drawing.Point(([int]($gx - $size / 2)), ([int]($gy - $size / 2)))

$f.Add_Paint({
    param($s, $e)
    $g = $e.Graphics
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $penW = [Math]::Max(3, [int]($size / 28))
    $pen = New-Object System.Drawing.Pen($mc, $penW)
    $brush = New-Object System.Drawing.SolidBrush($mc)
    $cx = [int]($size / 2); $cy = [int]($size / 2); $r = [int]($size / 2 - 10)
    $g.DrawEllipse($pen, ($cx - $r), ($cy - $r), (2 * $r), (2 * $r))
    $g.FillEllipse($brush, ($cx - 4), ($cy - 4), 8, 8)
    # crosshair ticks across the ring
    $g.DrawLine($pen, $cx, ($cy - $r - 7), $cx, ($cy - $r + 7))
    $g.DrawLine($pen, $cx, ($cy + $r - 7), $cx, ($cy + $r + 7))
    $g.DrawLine($pen, ($cx - $r - 7), $cy, ($cx - $r + 7), $cy)
    $g.DrawLine($pen, ($cx + $r - 7), $cy, ($cx + $r + 7), $cy)
    if ($Label -ne '') {
        $font = New-Object System.Drawing.Font('Consolas', 9, [System.Drawing.FontStyle]::Bold)
        $g.DrawString($Label, $font, $brush, 2, ($size - 2))
    }
})

$f.Add_Shown({
    $h = $f.Handle
    $GWL_EXSTYLE = -20
    $WS_EX_LAYERED = 0x80000
    $WS_EX_TRANSPARENT = 0x20
    $WS_EX_NOACTIVATE = 0x08000000
    $WS_EX_TOOLWINDOW = 0x80
    $ex = [NW]::GetWindowLong($h, $GWL_EXSTYLE)
    $ex = $ex -bor $WS_EX_LAYERED -bor $WS_EX_TRANSPARENT -bor $WS_EX_NOACTIVATE -bor $WS_EX_TOOLWINDOW
    [void][NW]::SetWindowLong($h, $GWL_EXSTYLE, $ex)
})

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = $Ms
$timer.Add_Tick({ $timer.Stop(); $f.Close() })
$timer.Start()

[System.Windows.Forms.Application]::Run($f)
