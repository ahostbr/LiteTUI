Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Storage.StorageFile, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
$f = [Windows.Storage.StorageFile]::GetFileFromPathAsync("F:\Projects\private\LMStudioChat\pccontrol\ocr.ps1").GetAwaiter().GetResult()
Write-Output ("OK: " + $f.Name)
