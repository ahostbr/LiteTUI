param(
    [int]$Monitor = -1,
    [string]$Output = '',
    [switch]$List
)

function Resolve-ScreenshotOutputPath {
    param([int]$Monitor, [string]$Output, [string]$ScriptDirectory, [string]$DataRoot)
    if ($Output -ne '') { return $Output }
    $directory = if ($DataRoot -ne '') {
        Join-Path ([IO.Path]::GetFullPath($DataRoot)) 'pccontrol'
    } else { $ScriptDirectory }
    $label = if ($Monitor -ge 0) { 'mon' + $Monitor } else { 'all' }
    return Join-Path $directory ($label + '.jpg')
}

function Initialize-ScreenshotOutputDirectory {
    param([string]$Path)
    $parent = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Path))
    [void][IO.Directory]::CreateDirectory($parent)
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$screens = [System.Windows.Forms.Screen]::AllScreens

if ($List) {
    for ($i = 0; $i -lt $screens.Count; $i++) {
        $s = $screens[$i]
        $tag = ''
        if ($s.Primary) { $tag = ' [PRIMARY]' }
        $w = $s.Bounds.Width
        $h = $s.Bounds.Height
        $x = $s.Bounds.X
        $y = $s.Bounds.Y
        Write-Output ('Monitor {0}{1}: {2}x{3} at ({4},{5}) - {6}' -f $i, $tag, $w, $h, $x, $y, $s.DeviceName)
    }
    exit 0
}

if ($Monitor -ge $screens.Count) {
    Write-Error ('Monitor {0} does not exist. Use -List to see available monitors.' -f $Monitor)
    exit 1
}

if ($Monitor -ge 0) {
    $bounds = $screens[$Monitor].Bounds
} else {
    $minX = ($screens | ForEach-Object { $_.Bounds.X } | Measure-Object -Minimum).Minimum
    $minY = ($screens | ForEach-Object { $_.Bounds.Y } | Measure-Object -Minimum).Minimum
    $maxX = ($screens | ForEach-Object { $_.Bounds.X + $_.Bounds.Width } | Measure-Object -Maximum).Maximum
    $maxY = ($screens | ForEach-Object { $_.Bounds.Y + $_.Bounds.Height } | Measure-Object -Maximum).Maximum
    $bounds = [System.Drawing.Rectangle]::FromLTRB($minX, $minY, $maxX, $maxY)
}

$Output = Resolve-ScreenshotOutputPath -Monitor $Monitor -Output $Output -ScriptDirectory $PSScriptRoot -DataRoot $env:LITETUI_DATA_ROOT
Initialize-ScreenshotOutputDirectory -Path $Output

$bitmap = New-Object System.Drawing.Bitmap($bounds.Width, $bounds.Height)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$graphics.Dispose()

$bitmap.Save($Output, [System.Drawing.Imaging.ImageFormat]::Jpeg)
$bitmap.Dispose()

Write-Output $Output
