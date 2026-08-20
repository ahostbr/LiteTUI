# ocr.ps1 — text out of an image, using the OS's own OCR engine (no installs)
# Run with PowerShell 7:  pwsh -NoProfile -File ocr.ps1 -Path img.jpg
param(
    [Parameter(Mandatory=$true)][string]$Path,
    [int]$MaxChars = 2000
)

# load the WinRT types we touch (PS needs them registered before use)
[Windows.Storage.StorageFile, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null

$file   = [Windows.Storage.StorageFile]::GetFileFromPathAsync($Path).AsTask().Result
$stream = $file.OpenAsync([Windows.Storage.FileAccessMode]::Read).AsTask().Result
if ($Path -imatch '\.(png|webp)$') { $decoderId = [Windows.Graphics.Imaging.BitmapDecoder]::PngDecoderId }
else                              { $decoderId = [Windows.Graphics.Imaging.BitmapDecoder]::JpegDecoderId }
$decoder = [Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($decoderId, $stream).AsTask().Result
$bitmap  = $decoder.DecodePixelDataAsync().AsTask().Result

$engine  = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguage()
if (-not $engine) { Write-Output "ERROR: no OCR engine"; exit 1 }
$result  = $engine.RecognizeAsync($bitmap).AsTask().Result

# lines in reading order (top-to-bottom, then left-to-right) so layout is guessable
$lines = foreach ($l in $result.Lines) {
    $r = $l.BoundingRect
    [PSCustomObject]@{ y = [int]$r.Y; x = [int]$r.X; text = $l.Text }
}
$text = ($lines | Sort-Object y, x | ForEach-Object { $_.text }) -join "  |  "
Write-Output $text.Substring(0, [Math]::Min($MaxChars, $text.Length))
