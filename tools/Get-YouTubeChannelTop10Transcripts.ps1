<#
.SYNOPSIS
Downloads English transcripts for all videos in a YouTube channel whose title
matches a phrase ("top 10" by default), and writes cleaned Markdown files.

.EXAMPLE
.\Get-YouTubeChannelTop10Transcripts.ps1 `
  -ChannelUrl 'https://www.youtube.com/@TheNextNewThingAI' `
  -OutputPath 'C:\Projects\LiteTUI\artifacts\youtube-top-10-transcripts'

.NOTES
Requires yt-dlp on PATH. Manual English subtitles are requested first; auto
English subtitles are also requested as a fallback. The original VTTs are kept
in the _source-vtt folder for verification.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $ChannelUrl,

    [string] $OutputPath = (Join-Path (Get-Location) 'youtube-transcripts'),

    # A PowerShell/.NET regular expression, evaluated case-insensitively.
    # Covers explicit “Top 10” titles and shortened weekly “10 GitHub Repos”
    # / “10 repos” titles such as “10 Github Repos That Solve...”.
    [string] $TitlePattern = '(?:top\s*10|\b10\b.*\b(?:github|repos?)\b)',

    # 0 means inspect the whole channel. Useful to limit a test run.
    [ValidateRange(0, [int]::MaxValue)]
    [int] $MaxVideos = 0,

    # Download again and regenerate Markdown even when a transcript exists.
    [switch] $Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-YtDlp {
    param([Parameter(Mandatory = $true)][string[]] $Arguments)
    $output = & yt-dlp @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "yt-dlp failed (exit $LASTEXITCODE): $($output -join [Environment]::NewLine)"
    }
    return $output
}

function Convert-VttToText {
    param([Parameter(Mandatory = $true)][string] $Path)

    $lines = Get-Content -LiteralPath $Path -Encoding UTF8
    $result = [System.Collections.Generic.List[string]]::new()
    $previous = ''
    $inIgnoredBlock = $false

    foreach ($rawLine in $lines) {
        $line = $rawLine.Trim()
        if ($line -match '^(NOTE|STYLE|REGION)(\s|$)') { $inIgnoredBlock = $true; continue }
        if (-not $line) { $inIgnoredBlock = $false; continue }
        if ($inIgnoredBlock -or $line -eq 'WEBVTT' -or $line -match '^Kind:' -or $line -match '^Language:') { continue }
        if ($line -match '-->') { continue }
        if ($line -match '^\d+$') { continue }

        # Strip VTT/HTML tags and collapse whitespace. Avoid repeating the
        # same caption when a VTT uses rolling caption windows.
        $line = [regex]::Replace($line, '<[^>]+>', '')
        $line = [System.Net.WebUtility]::HtmlDecode($line)
        $line = [regex]::Replace($line, '\s+', ' ').Trim()
        if ($line -and $line -ne $previous) {
            [void] $result.Add($line)
            $previous = $line
        }
    }
    return ($result -join ' ')
}

function Get-SafeStem {
    param([Parameter(Mandatory = $true)][string] $Title)
    $stem = [regex]::Replace($Title, '[<>:"/\\|?*\x00-\x1F]', '-')
    $stem = [regex]::Replace($stem, '\s+', ' ').Trim(' ', '.')
    if ($stem.Length -gt 100) { $stem = $stem.Substring(0, 100).Trim() }
    if (-not $stem) { $stem = 'untitled' }
    return $stem
}

if (-not (Get-Command yt-dlp -ErrorAction SilentlyContinue)) {
    throw 'yt-dlp was not found on PATH. Install it with: pip install yt-dlp'
}

$ChannelUrl = $ChannelUrl.TrimEnd('/')
if ($ChannelUrl -notmatch '/(videos|streams|shorts)$') { $ChannelUrl += '/videos' }

$OutputPath = [System.IO.Path]::GetFullPath($OutputPath)
$sourcePath = Join-Path $OutputPath '_source-vtt'
New-Item -ItemType Directory -Force -Path $OutputPath, $sourcePath | Out-Null

Write-Host "Inspecting $ChannelUrl ..."
$listingArgs = @('--flat-playlist', '--dump-single-json', '--no-warnings', $ChannelUrl)
if ($MaxVideos -gt 0) { $listingArgs = @('--playlist-end', $MaxVideos) + $listingArgs }
$listingText = (Invoke-YtDlp -Arguments $listingArgs) -join "`n"
$listing = $listingText | ConvertFrom-Json
$entries = @($listing.entries | Where-Object { $_ -and $_.title -match $TitlePattern })

$manifestPath = Join-Path $OutputPath 'matches.json'
$entries | ForEach-Object {
    [pscustomobject]@{
        id = [string]$_.id
        title = [string]$_.title
        url = "https://www.youtube.com/watch?v=$([string]$_.id)"
    }
} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
Write-Host ("Found {0} matching video(s) using /{1}/." -f $entries.Count, $TitlePattern)

$completed = [System.Collections.Generic.List[object]]::new()
$failed = [System.Collections.Generic.List[object]]::new()

foreach ($entry in $entries) {
    $id = [string]$entry.id
    $title = [string]$entry.title
    # Flat channel entries do not reliably expose webpage_url; constructing the
    # canonical watch URL from the video id is stable for every entry shape.
    $url = "https://www.youtube.com/watch?v=$id"
    $stem = "$id--$(Get-SafeStem $title)"
    $markdownPath = Join-Path $OutputPath "$stem.md"

    if ((Test-Path -LiteralPath $markdownPath) -and -not $Force) {
        Write-Host "[skip] $title"
        [void] $completed.Add([pscustomobject]@{ id = $id; title = $title; url = $url; file = [System.IO.Path]::GetFileName($markdownPath); status = 'already present' })
        continue
    }

    Write-Host "[get]  $title"
    try {
        # --write-subs obtains creator captions; --write-auto-subs supplies a
        # fallback. Only English and English regional variants are requested.
        Invoke-YtDlp -Arguments @(
            '--skip-download', '--write-subs', '--write-auto-subs',
            '--sub-langs', 'en.*,en', '--convert-subs', 'vtt', '--no-overwrites',
            '--no-warnings', '-o', (Join-Path $sourcePath '%(id)s.%(ext)s'), $url
        ) | Out-Null

        $vtts = @(Get-ChildItem -LiteralPath $sourcePath -File -Filter "$id*.vtt" | Sort-Object LastWriteTime -Descending)
        if ($vtts.Count -eq 0) { throw 'No English VTT subtitle was available.' }

        # Prefer an exact English file, otherwise use the newest English variant.
        $vtt = @($vtts | Where-Object { $_.Name -match "^$([regex]::Escape($id))\.en\.vtt$" } | Select-Object -First 1)
        if ($vtt.Count -eq 0) { $vtt = @($vtts | Select-Object -First 1) }
        $transcript = Convert-VttToText -Path $vtt[0].FullName
        if (-not $transcript) { throw "Subtitle file '$($vtt[0].Name)' had no spoken text." }

        $markdown = @(
            "# $title",
            '',
            "- **Video:** $url",
            "- **Video ID:** $id",
            "- **Transcript source:** $($vtt[0].Name)",
            "- **Retrieved (UTC):** $([DateTime]::UtcNow.ToString('o'))",
            '',
            '## Transcript',
            '',
            $transcript,
            ''
        ) -join "`n"
        Set-Content -LiteralPath $markdownPath -Value $markdown -Encoding UTF8
        [void] $completed.Add([pscustomobject]@{ id = $id; title = $title; url = $url; file = [System.IO.Path]::GetFileName($markdownPath); status = 'downloaded' })
    }
    catch {
        Write-Warning "Failed: $title -- $($_.Exception.Message)"
        [void] $failed.Add([pscustomobject]@{ id = $id; title = $title; url = $url; error = $_.Exception.Message })
    }
}

$index = [System.Collections.Generic.List[string]]::new()
[void] $index.Add('# YouTube “Top 10” transcript export')
[void] $index.Add('')
[void] $index.Add("- Channel: $ChannelUrl")
[void] $index.Add(('- Title regex: `{0}`' -f $TitlePattern))
[void] $index.Add("- Matching videos: $($entries.Count)")
[void] $index.Add("- Transcripts available: $($completed.Count)")
[void] $index.Add("- Failures: $($failed.Count)")
[void] $index.Add('')
[void] $index.Add('## Transcripts')
[void] $index.Add('')
foreach ($item in $completed) { [void] $index.Add("- [$($item.title)]($($item.file)) — $($item.status)") }
if ($failed.Count -gt 0) {
    [void] $index.Add('')
    [void] $index.Add('## Videos without a transcript')
    [void] $index.Add('')
    foreach ($item in $failed) { [void] $index.Add("- [$($item.title)]($($item.url)) — $($item.error)") }
}
Set-Content -LiteralPath (Join-Path $OutputPath 'INDEX.md') -Value ($index -join "`n") -Encoding UTF8

[pscustomobject]@{
    Channel = $ChannelUrl
    TitlePattern = $TitlePattern
    Matches = $entries.Count
    Available = $completed.Count
    Failed = $failed.Count
    OutputPath = $OutputPath
    Index = (Join-Path $OutputPath 'INDEX.md')
}
