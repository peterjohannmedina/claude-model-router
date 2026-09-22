[CmdletBinding()]
param(
    [string]$BaseUrl = $(if ($env:GANGLION_BASE_URL) { $env:GANGLION_BASE_URL } else { 'http://127.0.0.1:8471/v1' }),
    [string]$Model = $(if ($env:GANGLION_MODEL) { $env:GANGLION_MODEL } else { 'ganglion' }),
    [string]$ApiKeyEnv = $(if ($env:GANGLION_API_KEY_ENV) { $env:GANGLION_API_KEY_ENV } else { 'HELIOS_API_TOKEN' }),
    [ValidateSet('ChatCompletions', 'Responses')]
    [string]$WireApi = $(if ($env:GANGLION_WIRE_API) { $env:GANGLION_WIRE_API } else { 'ChatCompletions' }),
    [ValidateRange(1, 4096)]
    [int]$MaxTokens = 32,
    [ValidateRange(1, 600)]
    [int]$TimeoutSec = 30,
    [switch]$SkipCompletion
)

$ErrorActionPreference = 'Stop'
$root = $BaseUrl.TrimEnd('/')
$token = [Environment]::GetEnvironmentVariable($ApiKeyEnv)
if ([string]::IsNullOrWhiteSpace($token)) {
    throw "Ganglion probe requires the '$ApiKeyEnv' environment variable."
}

$headers = @{ Authorization = "Bearer $token" }

try {
    $models = Invoke-RestMethod -Uri "$root/models" -Headers $headers -Method Get -TimeoutSec $TimeoutSec
} catch {
    throw "Ganglion model discovery failed at $root/models: $($_.Exception.Message)"
}

$modelIds = @($models.data | ForEach-Object { [string]$_.id } | Where-Object { $_ })
if ($modelIds -notcontains $Model) {
    $advertised = if ($modelIds.Count) { $modelIds -join ', ' } else { '<none>' }
    throw "Ganglion did not advertise model '$Model'. Advertised models: $advertised"
}

if ($SkipCompletion) {
    Write-Output ("Ganglion catalog OK: wire_api={0}; base_url={1}; model={2}; models={3}" -f $WireApi, $root, $Model, ($modelIds -join ', '))
    exit 0
}

if ($WireApi -eq 'Responses') {
    $uri = "$root/responses"
    $payload = @{
        model = $Model
        input = 'Reply with exactly GANGLION_READY and nothing else.'
        max_output_tokens = $MaxTokens
        store = $false
        stream = $false
    }
} else {
    $uri = "$root/chat/completions"
    $payload = @{
        model = $Model
        messages = @(
            @{ role = 'user'; content = 'Reply with exactly GANGLION_READY and nothing else.' }
        )
        max_tokens = $MaxTokens
        temperature = 0
        reasoning_effort = 'none'
        stream = $false
    }
}

try {
    $completion = Invoke-RestMethod -Uri $uri -Headers $headers -Method Post -ContentType 'application/json' -Body ($payload | ConvertTo-Json -Depth 10) -TimeoutSec $TimeoutSec
} catch {
    throw "Ganglion $WireApi completion failed at ${uri}: $($_.Exception.Message)"
}

$text = ''
if ($WireApi -eq 'Responses') {
    $text = [string]$completion.output_text
    if ([string]::IsNullOrWhiteSpace($text)) {
        $parts = @($completion.output | ForEach-Object { $_.content } | ForEach-Object { $_.text })
        $text = ($parts -join '')
    }
} else {
    $text = [string]$completion.choices[0].message.content
}
if ([string]::IsNullOrWhiteSpace($text)) {
    throw "Ganglion $WireApi completion returned no usable text."
}

Write-Output ("Ganglion probe OK: wire_api={0}; base_url={1}; model={2}; models={3}; completion=usable" -f $WireApi, $root, $Model, ($modelIds -join ', '))
