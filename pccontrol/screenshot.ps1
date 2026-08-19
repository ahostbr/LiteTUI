param(
    [int]$Monitor = -1,
    [string]$Output = '',
    [switch]$List
)

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

$bitmap = New-Object System.Drawing.Bitmap($bounds.Width, $bounds.Height)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$graphics.Dispose()

if ($Output -eq '') {
    $skillDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    if ($Monitor -ge 0) { $monLabel = 'mon' + $Monitor } else { $monLabel = 'all' }
    $Output = Join-Path $skillDir ($monLabel + '.jpg')
}

$bitmap.Save($Output, [System.Drawing.Imaging.ImageFormat]::Jpeg)
$bitmap.Dispose()

Write-Output $Output
