<#
.SYNOPSIS
Creates detailed companion summaries for exported YouTube transcripts through
an OpenAI-compatible LM Studio server, then rebuilds INDEX.md with links to
both versions.

.EXAMPLE
.\New-YouTubeTranscriptSummaries.ps1 `
  -TranscriptPath 'C:\Projects\LiteTUI\artifacts\youtube-top-10-transcripts'

.NOTES
The default model is qwen/qwen3.5-9b at LM Studio's local API endpoint.
Summaries are written to a sibling summaries folder; full transcript Markdown
files are never altered. Existing summaries are skipped unless -Force is used.
#>
[CmdletBinding()]
param(
    [string] $TranscriptPath = 'C:\Projects\LiteTUI\artifacts\youtube-top-10-transcripts',
    [string] $ApiBaseUrl = 'http://localhost:1234/v1',
    [string] $Model = 'qwen/qwen3.5-9b',
    [ValidateRange(512, 16384)]
    [int] $MaxTokens = 5000,
    [switch] $Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-TranscriptInfo {
    param([Parameter(Mandatory = $true)][System.IO.FileInfo] $File)
    $content = Get-Content -LiteralPath $File.FullName -Raw -Encoding UTF8
    $titleMatch = [regex]::Match($content, '(?m)^#\s+(.+?)\s*$')
    $urlMatch = [regex]::Match($content, '(?m)^- \*\*Video:\*\*\s*(\S+)\s*$')
    if (-not $titleMatch.Success) { throw "No H1 title in '$($File.Name)'." }
    [pscustomobject]@{
        File = $File
        Title = $titleMatch.Groups[1].Value.Trim()
        Url = if ($urlMatch.Success) { $urlMatch.Groups[1].Value.Trim() } else { '' }
        Content = $content
    }
}

function Invoke-LmStudioSummary {
    param(
        [Parameter(Mandatory = $true)][string] $Title,
        [Parameter(Mandatory = $true)][string] $Transcript
    )
    $system = @'
You are a meticulous research assistant. Produce a detailed, self-contained Markdown summary of the supplied YouTube transcript. Preserve factual qualifications and uncertainty. Do not invent repositories, links, features, numbers, or conclusions absent from the transcript.

Use exactly this structure:
## Executive summary
A substantial explanation of the video's main point and practical takeaway.

## Projects, tools, and ideas covered
For every distinct tool, repository, product, or major idea discussed, add a ### heading. Explain what it is, how the speaker says it works, concrete capabilities, caveats, pricing/availability if mentioned, and why someone would use it. Do not omit items merely because the list is long.

## Recommendations and workflows
Capture actionable steps, configurations, comparisons, and selection guidance presented.

## Caveats and verification notes
Capture limitations, sponsorship/disclosure, risks, unclear claims, or points the viewer should independently verify. Write "None stated in the transcript." only when appropriate.

## Bottom line
Give a concise but specific closing assessment grounded only in this transcript.
'@
    $user = "Video title: $Title`n`nTranscript follows:`n`n$Transcript"
    $payload = @{
        model = $Model
        messages = @(
            @{ role = 'system'; content = $system },
            @{ role = 'user'; content = $user }
        )
        temperature = 0.15
        max_tokens = $MaxTokens
        stream = $false
    } | ConvertTo-Json -Depth 8
    try {
        $response = Invoke-RestMethod -Method Post -Uri "$($ApiBaseUrl.TrimEnd('/'))/chat/completions" -ContentType 'application/json; charset=utf-8' -Body $payload -TimeoutSec 1800
    }
    catch {
        throw "LM Studio request failed: $($_.Exception.Message)"
    }
    if (-not $response.choices -or -not $response.choices[0].message.content) {
        throw 'LM Studio returned no chat-completion content.'
    }
    return [string]$response.choices[0].message.content
}

$TranscriptPath = [System.IO.Path]::GetFullPath($TranscriptPath)
if (-not (Test-Path -LiteralPath $TranscriptPath -PathType Container)) {
    throw "Transcript directory does not exist: $TranscriptPath"
}
$summaryDir = Join-Path $TranscriptPath 'summaries'
New-Item -ItemType Directory -Force -Path $summaryDir | Out-Null

# Fail early with an intelligible error if LM Studio is unreachable or the
# selected model isn't exposed by its OpenAI-compatible API.
try {
    $models = Invoke-RestMethod -Uri "$($ApiBaseUrl.TrimEnd('/'))/models" -TimeoutSec 15
    if (-not @($models.data.id) -contains $Model) { throw "Model '$Model' was not returned by $ApiBaseUrl/models." }
}
catch { throw "Cannot use LM Studio at $ApiBaseUrl. $($_.Exception.Message)" }

$fullFiles = @(Get-ChildItem -LiteralPath $TranscriptPath -File -Filter '*.md' |
    Where-Object { $_.Name -ne 'INDEX.md' } | Sort-Object Name)
if ($fullFiles.Count -eq 0) { throw "No transcript Markdown files in $TranscriptPath" }

$complete = [System.Collections.Generic.List[object]]::new()
$failed = [System.Collections.Generic.List[object]]::new()
foreach ($file in $fullFiles) {
    $info = Get-TranscriptInfo -File $file
    $summaryName = "$($file.BaseName).summary.md"
    $summaryPath = Join-Path $summaryDir $summaryName
    if ((Test-Path -LiteralPath $summaryPath) -and -not $Force) {
        Write-Host "[skip] $($info.Title)"
        [void]$complete.Add([pscustomobject]@{ Info = $info; SummaryName = $summaryName; Status = 'already present' })
        continue
    }

    Write-Host "[summarize] $($info.Title)"
    try {
        $summary = Invoke-LmStudioSummary -Title $info.Title -Transcript $info.Content
        $document = @(
            "# Detailed summary — $($info.Title)",
            '',
            "- **Video:** $($info.Url)",
            "- **Full transcript:** [../$($file.Name)](../$($file.Name))",
            "- **Model:** $Model",
            "- **Generated (UTC):** $([DateTime]::UtcNow.ToString('o'))",
            '',
            $summary.Trim(),
            ''
        ) -join "`n"
        Set-Content -LiteralPath $summaryPath -Value $document -Encoding UTF8
        [void]$complete.Add([pscustomobject]@{ Info = $info; SummaryName = $summaryName; Status = 'generated' })
    }
    catch {
        Write-Warning "Failed: $($info.Title) -- $($_.Exception.Message)"
        [void]$failed.Add([pscustomobject]@{ Info = $info; Error = $_.Exception.Message })
    }
}

# Rebuild the landing page from actual files so its links remain correct after
# a resumable run. The source files and source caption VTTs are left untouched.
$index = [System.Collections.Generic.List[string]]::new()
[void]$index.Add('# YouTube “Top 10” transcript export')
[void]$index.Add('')
[void]$index.Add('- Channel: https://www.youtube.com/@TheNextNewThingAI/videos')
[void]$index.Add('- Transcript selection: titles matching `(?:top\s*10|\b10\b.*\b(?:github|repos?)\b)`')
[void]$index.Add("- Full transcripts: $($fullFiles.Count)")
[void]$index.Add("- Detailed summaries available: $($complete.Count)")
[void]$index.Add("- Summary failures: $($failed.Count)")
[void]$index.Add(('- Summary model: `{0}`' -f $Model))
[void]$index.Add('')
[void]$index.Add('## Videos')
[void]$index.Add('')
foreach ($item in $complete) {
    $fullName = $item.Info.File.Name
    [void]$index.Add("- **$($item.Info.Title)** — [full transcript]($fullName) · [detailed summary](summaries/$($item.SummaryName)) — $($item.Status)")
}
if ($failed.Count -gt 0) {
    [void]$index.Add('')
    [void]$index.Add('## Summary failures')
    [void]$index.Add('')
    foreach ($item in $failed) { [void]$index.Add("- **$($item.Info.Title)** — [full transcript]($($item.Info.File.Name)); error: $($item.Error)") }
}
Set-Content -LiteralPath (Join-Path $TranscriptPath 'INDEX.md') -Value ($index -join "`n") -Encoding UTF8

[pscustomobject]@{
    TranscriptPath = $TranscriptPath
    FullTranscripts = $fullFiles.Count
    Summaries = $complete.Count
    Failures = $failed.Count
    SummaryPath = $summaryDir
    Index = (Join-Path $TranscriptPath 'INDEX.md')
}
