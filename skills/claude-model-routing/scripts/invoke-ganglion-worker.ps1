[CmdletBinding()]
param(
    [string]$Prompt,
    [Parameter(Mandatory = $true)][string]$BaseUrl,
    [Parameter(Mandatory = $true)][string]$Model,
    [Parameter(Mandatory = $true)][string]$ApiKeyEnv,
    [ValidateSet('ChatCompletions', 'Responses')][string]$WireApi = 'ChatCompletions',
    [ValidateRange(1, 4096)][int]$MaxTokens = 512,
    [ValidateRange(1, 1800)][int]$TimeoutSec = 300
)

$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
if ([string]::IsNullOrWhiteSpace($Prompt) -and [Console]::IsInputRedirected) {
    $Prompt = [Console]::In.ReadToEnd()
}
if ([string]::IsNullOrWhiteSpace($Prompt)) {
    throw 'A bounded task packet is required through -Prompt or redirected standard input.'
}
if ($Prompt.Length -gt 50000) {
    throw 'Task packet exceeds the 50000-character input limit.'
}

$token = [Environment]::GetEnvironmentVariable($ApiKeyEnv)
if ([string]::IsNullOrWhiteSpace($token)) {
    throw "Ganglion worker requires the '$ApiKeyEnv' environment variable."
}

$root = $BaseUrl.TrimEnd('/')
$headers = @{ Authorization = "Bearer $token" }
if ($WireApi -eq 'Responses') {
    $uri = "$root/responses"
    $payload = @{
        model = $Model
        input = @(
            @{ role = 'developer'; content = 'You are a bounded local worker. Complete only the supplied task packet. Return concise evidence and uncertainty. Do not edit files, use tools, request secrets, or broaden scope.' },
            @{ role = 'user'; content = $Prompt }
        )
        max_output_tokens = $MaxTokens
        store = $false
        stream = $false
    }
} else {
    $uri = "$root/chat/completions"
    $payload = @{
        model = $Model
        messages = @(
            @{ role = 'system'; content = 'You are a bounded local worker. Complete only the supplied task packet. Return concise evidence and uncertainty. Do not edit files, use tools, request secrets, or broaden scope.' },
            @{ role = 'user'; content = $Prompt }
        )
        max_tokens = $MaxTokens
        temperature = 0
        reasoning_effort = 'none'
        stream = $false
    }
}

try {
    $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes(($payload | ConvertTo-Json -Depth 10))
    $completion = Invoke-RestMethod -Uri $uri -Headers $headers -Method Post -ContentType 'application/json; charset=utf-8' -Body $bodyBytes -TimeoutSec $TimeoutSec
} catch {
    throw "Ganglion worker completion failed at ${uri}: $($_.Exception.Message)"
}

if ($WireApi -eq 'Responses') {
    $text = [string]$completion.output_text
    if ([string]::IsNullOrWhiteSpace($text)) {
        $text = (@($completion.output | ForEach-Object { $_.content } | ForEach-Object { $_.text }) -join '')
    }
} else {
    $content = $completion.choices[0].message.content
    if ($content -is [string]) {
        $text = $content
    } else {
        $text = (@($content | ForEach-Object { if ($_ -is [string]) { $_ } elseif ($_.text) { [string]$_.text } }) -join '')
    }
}
if ([string]::IsNullOrWhiteSpace($text)) {
    throw 'Ganglion worker returned no usable assistant text.'
}

Write-Output $text.Trim()
