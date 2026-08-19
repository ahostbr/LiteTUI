Add-Type -AssemblyName System.Runtime.WindowsRuntime 
# ocr.ps1 — text out of an image, using the OS's own OCR engine (no installs)
param(
    [Parameter(Mandatory=$true)][string]$Path,
    [int]$MaxChars = 2000
)

# --- WinRT/PS5.1 bridge: make .GetAwaiter() work on async COM results ---
[Windows.Storage.StorageFile, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
$asTaskGeneric = ([System.Windows.RuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 } |
    Where-Object { $null -ne $_.GetParameters()[0].ParameterType.Name })[0]
function Await($in, $type) { $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($in)) }

$file   = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.DataStream])
if ($Path -imatch '\.(png|webp)$') { $decoderId = [Windows.Graphics.Imaging.BitmapDecoder]::PngDecoderId }
else                              { $decoderId = [Windows.Graphics.Imaging.BitmapDecoder]::JpegDecoderId }
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($decoderId, $stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap  = Await ($decoder.DecodePixelDataAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
$engine  = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguage()
if (-not $engine) { Write-Output "ERROR: no OCR engine"; exit 1 }
$result  = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

# lines in reading order (top-to-bottom, then left-to-right) so layout is guessable
$lines = foreach ($l in $result.Lines) {
    $r = $l.BoundingRect
    [PSCustomObject]@{ y = [int]$r.Y; x = [int]$r.X; text = $l.Text }
}
$text = ($lines | Sort-Object y, x | ForEach-Object { $_.text }) -join "  |  "
Write-Output $text.Substring(0, [Math]::Min($MaxChars, $text.Length))
