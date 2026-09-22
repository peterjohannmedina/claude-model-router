[CmdletBinding()]
param(
    [string]$LocalBaseUrl = $(if ($env:GANGLION_BASE_URL) { $env:GANGLION_BASE_URL } else { 'http://127.0.0.1:8471/v1' }),
    [string]$LocalModel = $(if ($env:GANGLION_MODEL) { $env:GANGLION_MODEL } else { 'ganglion' }),
    [string]$LocalApiKeyEnv = $(if ($env:GANGLION_API_KEY_ENV) { $env:GANGLION_API_KEY_ENV } else { 'HELIOS_API_TOKEN' }),
    [string]$ContinuityBaseUrl = $(if ($env:GANGLION_CONTINUITY_BASE_URL) { $env:GANGLION_CONTINUITY_BASE_URL } else { 'http://127.0.0.1:8472/v1' }),
    [string]$ContinuityModel = $(if ($env:GANGLION_CONTINUITY_MODEL) { $env:GANGLION_CONTINUITY_MODEL } else { 'ganglion' }),
    [string]$ContinuityApiKeyEnv = $(if ($env:GANGLION_CONTINUITY_API_KEY_ENV) { $env:GANGLION_CONTINUITY_API_KEY_ENV } else { $LocalApiKeyEnv }),
    [string]$GatewayBaseUrl = $(if ($env:GANGLION_GATEWAY_BASE_URL) { $env:GANGLION_GATEWAY_BASE_URL } else { '' }),
    [string]$GatewayModel = $(if ($env:GANGLION_GATEWAY_MODEL) { $env:GANGLION_GATEWAY_MODEL } else { 'ganglion-auto' }),
    [string]$GatewayApiKeyEnv = $(if ($env:GANGLION_GATEWAY_API_KEY_ENV) { $env:GANGLION_GATEWAY_API_KEY_ENV } else { 'GANGLION_API_KEY' }),
    [ValidateRange(1, 60)]
    [int]$DiscoveryTimeoutSec = 5,
    [ValidateRange(1, 1800)]
    [int]$CompletionTimeoutSec = 180,
    [ValidateRange(1, 4096)]
    [int]$MaxTokens = 32,
    [switch]$SkipCompletion,
    [ValidateSet('Json', 'Text')]
    [string]$OutputFormat = 'Json'
)

$ErrorActionPreference = 'Stop'

function Get-PropertyValue {
    param(
        [AllowNull()]
        [object]$Object,
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    if ($null -eq $Object) {
        return $null
    }

    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $null
    }

    return $property.Value
}

function Get-NumberValue {
    param(
        [AllowNull()]
        [object]$Object,
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    $value = Get-PropertyValue -Object $Object -Name $Name
    if ($null -eq $value -or [string]::IsNullOrWhiteSpace([string]$value)) {
        return $null
    }

    try {
        return [double]$value
    } catch {
        return $null
    }
}

function Normalize-BaseUrl {
    param([Parameter(Mandatory = $true)][string]$BaseUrl)
    return $BaseUrl.TrimEnd('/')
}

function Get-AuthHeaders {
    param([Parameter(Mandatory = $true)][string]$ApiKeyEnv)

    $token = [Environment]::GetEnvironmentVariable($ApiKeyEnv)
    if ([string]::IsNullOrWhiteSpace($token)) {
        return [pscustomobject]@{
            ok = $false
            headers = $null
            error = "The '$ApiKeyEnv' environment variable is not set."
        }
    }

    return [pscustomobject]@{
        ok = $true
        headers = @{ Authorization = "Bearer $token" }
        error = $null
    }
}

function Invoke-JsonGet {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][hashtable]$Headers,
        [Parameter(Mandatory = $true)][int]$TimeoutSec
    )

    try {
        $value = Invoke-RestMethod -Uri $Uri -Headers $Headers -Method Get -TimeoutSec $TimeoutSec
        return [pscustomobject]@{ ok = $true; value = $value; error = $null }
    } catch {
        return [pscustomobject]@{ ok = $false; value = $null; error = $_.Exception.Message }
    }
}

function Invoke-JsonPost {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][hashtable]$Headers,
        [Parameter(Mandatory = $true)][object]$Payload,
        [Parameter(Mandatory = $true)][int]$TimeoutSec
    )

    try {
        $body = $Payload | ConvertTo-Json -Depth 12
        $value = Invoke-RestMethod -Uri $Uri -Headers $Headers -Method Post -ContentType 'application/json' -Body $body -TimeoutSec $TimeoutSec
        return [pscustomobject]@{ ok = $true; value = $value; error = $null }
    } catch {
        return [pscustomobject]@{ ok = $false; value = $null; error = $_.Exception.Message }
    }
}

function Get-ModelIds {
    param([AllowNull()][object]$Models)
    return @($Models.data | ForEach-Object { [string](Get-PropertyValue -Object $_ -Name 'id') } | Where-Object { $_ })
}

function Get-UsableCompletionText {
    param(
        [AllowNull()][object]$Completion,
        [Parameter(Mandatory = $true)][ValidateSet('ChatCompletions', 'Responses')][string]$WireApi
    )

    if ($WireApi -eq 'Responses') {
        $outputText = [string](Get-PropertyValue -Object $Completion -Name 'output_text')
        if (-not [string]::IsNullOrWhiteSpace($outputText)) {
            return $outputText
        }

        $parts = @()
        foreach ($item in @((Get-PropertyValue -Object $Completion -Name 'output'))) {
            foreach ($content in @((Get-PropertyValue -Object $item -Name 'content'))) {
                $text = [string](Get-PropertyValue -Object $content -Name 'text')
                if (-not [string]::IsNullOrWhiteSpace($text)) {
                    $parts += $text
                }
            }
        }
        return ($parts -join '')
    }

    $choices = @((Get-PropertyValue -Object $Completion -Name 'choices'))
    if ($choices.Count -eq 0) {
        return ''
    }

    $message = Get-PropertyValue -Object $choices[0] -Name 'message'
    $content = Get-PropertyValue -Object $message -Name 'content'
    if ($content -is [string]) {
        return $content
    }

    $parts = @()
    foreach ($part in @($content)) {
        if ($part -is [string]) {
            $parts += $part
        } else {
            $text = [string](Get-PropertyValue -Object $part -Name 'text')
            if (-not [string]::IsNullOrWhiteSpace($text)) {
                $parts += $text
            }
        }
    }
    return ($parts -join '')
}

function Test-Completion {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [Parameter(Mandatory = $true)][string]$Model,
        [Parameter(Mandatory = $true)][hashtable]$Headers,
        [Parameter(Mandatory = $true)][ValidateSet('ChatCompletions', 'Responses')][string]$WireApi,
        [Parameter(Mandatory = $true)][int]$TimeoutSec,
        [Parameter(Mandatory = $true)][int]$MaxTokens
    )

    if ($WireApi -eq 'Responses') {
        $uri = "$(Normalize-BaseUrl -BaseUrl $BaseUrl)/responses"
        $payload = @{
            model = $Model
            input = 'Reply with exactly GANGLION_CASCADE_READY and nothing else.'
            max_output_tokens = $MaxTokens
            store = $false
            stream = $false
        }
    } else {
        $uri = "$(Normalize-BaseUrl -BaseUrl $BaseUrl)/chat/completions"
        $payload = @{
            model = $Model
            messages = @(
                @{ role = 'user'; content = 'Reply with exactly GANGLION_CASCADE_READY and nothing else.' }
            )
            max_tokens = $MaxTokens
            temperature = 0
            reasoning_effort = 'none'
            stream = $false
        }
    }

    $response = Invoke-JsonPost -Uri $uri -Headers $Headers -Payload $payload -TimeoutSec $TimeoutSec
    if (-not $response.ok) {
        return [pscustomobject]@{ ok = $false; error = $response.error }
    }

    $text = Get-UsableCompletionText -Completion $response.value -WireApi $WireApi
    if ([string]::IsNullOrWhiteSpace($text)) {
        return [pscustomobject]@{ ok = $false; error = 'The completion returned no usable text.' }
    }

    return [pscustomobject]@{ ok = $true; error = $null }
}

function Add-QueueDepths {
    param(
        [AllowNull()][object]$QueueData,
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [System.Collections.Generic.List[double]]$Depths
    )

    if ($null -eq $QueueData) {
        return
    }

    foreach ($item in @($QueueData)) {
        $depth = Get-NumberValue -Object $item -Name 'depth'
        if ($null -ne $depth) {
            [void]$Depths.Add($depth)
            continue
        }

        $properties = @($item.PSObject.Properties)
        if ($properties.Count -eq 0) {
            continue
        }

        foreach ($property in $properties) {
            $nestedDepth = Get-NumberValue -Object $item -Name $property.Name
            if ($null -ne $nestedDepth) {
                [void]$Depths.Add($nestedDepth)
            }
        }
    }
}

function Get-RuntimeInstances {
    param([AllowNull()][object]$Payload)

    $nested = Get-PropertyValue -Object $Payload -Name 'runtime_instances'
    if ($null -ne $nested) {
        return @($nested)
    }

    return @($Payload)
}

function Get-CapacityAssessment {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [Parameter(Mandatory = $true)][hashtable]$Headers,
        [Parameter(Mandatory = $true)][int]$TimeoutSec
    )

    $root = Normalize-BaseUrl -BaseUrl $BaseUrl
    $runtimeResponse = Invoke-JsonGet -Uri "$root/runtime-instances" -Headers $Headers -TimeoutSec $TimeoutSec
    $metricsResponse = Invoke-JsonGet -Uri "$root/metrics" -Headers $Headers -TimeoutSec $TimeoutSec
    $graphResponse = $null

    $instances = @()
    if ($runtimeResponse.ok) {
        $instances = Get-RuntimeInstances -Payload $runtimeResponse.value
    } else {
        $graphResponse = Invoke-JsonGet -Uri "$root/resource-graph" -Headers $Headers -TimeoutSec $TimeoutSec
        if ($graphResponse.ok) {
            $instances = Get-RuntimeInstances -Payload $graphResponse.value
        }
    }

    if ($instances.Count -eq 0) {
        return [pscustomobject]@{
            state = 'unavailable'
            reason = 'No runtime instance was reported by Ganglion.'
            runtime_count = 0
            busy_signals = @()
            queue_depth = 0
        }
    }

    $busySignals = New-Object System.Collections.Generic.List[string]
    $unavailableSignals = New-Object System.Collections.Generic.List[string]
    $activeCount = 0
    $totalQueueDepth = 0.0

    foreach ($instance in $instances) {
        $instanceId = [string](Get-PropertyValue -Object $instance -Name 'id')
        if ([string]::IsNullOrWhiteSpace($instanceId)) {
            $instanceId = 'runtime'
        }

        $lifecycle = ([string](Get-PropertyValue -Object $instance -Name 'lifecycle')).ToLowerInvariant()
        $brokerState = ([string](Get-PropertyValue -Object $instance -Name 'broker_state')).ToLowerInvariant()
        $stateNames = @($lifecycle, $brokerState) | Where-Object { $_ }

        if ($stateNames | Where-Object { $_ -in @('offline', 'error', 'failed', 'stopped', 'unavailable') }) {
            [void]$unavailableSignals.Add("$instanceId is unavailable")
            continue
        }

        $activeCount++
        if ($stateNames | Where-Object { $_ -in @('loading', 'starting', 'warming', 'draining', 'busy') }) {
            [void]$busySignals.Add("$instanceId is $($stateNames -join '/')")
        }

        $inFlight = Get-NumberValue -Object $instance -Name 'in_flight'
        $leasedSlots = Get-NumberValue -Object $instance -Name 'leased_slots'
        $queueDepth = Get-NumberValue -Object $instance -Name 'queue_depth'
        $loadRatio = Get-NumberValue -Object $instance -Name 'replica_load_ratio'
        $concurrency = Get-NumberValue -Object $instance -Name 'concurrency_limit'

        if ($null -ne $inFlight -and $inFlight -gt 0) {
            [void]$busySignals.Add("$instanceId has $inFlight in-flight request(s)")
        }
        if ($null -ne $leasedSlots -and $leasedSlots -gt 0) {
            [void]$busySignals.Add("$instanceId has $leasedSlots leased slot(s)")
        }
        if ($null -ne $queueDepth -and $queueDepth -gt 0) {
            $totalQueueDepth += $queueDepth
            [void]$busySignals.Add("$instanceId has queue depth $queueDepth")
        }
        if ($null -ne $loadRatio -and $null -ne $concurrency -and $concurrency -gt 0 -and $loadRatio -ge 0.999) {
            [void]$busySignals.Add("$instanceId is at its concurrency limit")
        }
    }

    $depths = New-Object System.Collections.Generic.List[double]
    if ($metricsResponse.ok) {
        Add-QueueDepths -QueueData (Get-PropertyValue -Object $metricsResponse.value -Name 'queues') -Depths $depths
        Add-QueueDepths -QueueData (Get-PropertyValue -Object $metricsResponse.value -Name 'queues_by_instance') -Depths $depths
    }
    if ($graphResponse -and $graphResponse.ok) {
        Add-QueueDepths -QueueData (Get-PropertyValue -Object $graphResponse.value -Name 'queues') -Depths $depths
    }
    foreach ($depth in $depths) {
        if ($depth -gt 0) {
            $totalQueueDepth += $depth
            [void]$busySignals.Add("Ganglion queue depth is $depth")
        }
    }

    if ($activeCount -eq 0) {
        return [pscustomobject]@{
            state = 'unavailable'
            reason = ($unavailableSignals -join '; ')
            runtime_count = $instances.Count
            busy_signals = @($busySignals)
            queue_depth = $totalQueueDepth
        }
    }

    if ($busySignals.Count -gt 0) {
        return [pscustomobject]@{
            state = 'busy'
            reason = ($busySignals -join '; ')
            runtime_count = $instances.Count
            busy_signals = @($busySignals)
            queue_depth = $totalQueueDepth
        }
    }

    return [pscustomobject]@{
        state = 'ready'
        reason = 'Ganglion reported an active runtime with no in-flight, leased, or queued work.'
        runtime_count = $instances.Count
        busy_signals = @()
        queue_depth = $totalQueueDepth
    }
}

function New-CheckResult {
    param(
        [Parameter(Mandatory = $true)][object]$Candidate,
        [Parameter(Mandatory = $true)][string]$State,
        [Parameter(Mandatory = $true)][string]$Reason,
        [string]$CapacityState,
        [int]$RuntimeCount = 0,
        [double]$QueueDepth = 0,
        [bool]$CompletionChecked = $false
    )

    return [pscustomobject][ordered]@{
        id = $Candidate.id
        order = $Candidate.order
        scope = $Candidate.scope
        base_url = ([string]$Candidate.base_url).TrimEnd('/')
        model = $Candidate.model
        wire_api = $Candidate.wire_api
        api_key_env = $Candidate.api_key_env
        state = $State
        reason = $Reason
        capacity_state = $CapacityState
        runtime_count = $RuntimeCount
        queue_depth = $QueueDepth
        completion_checked = $CompletionChecked
    }
}

function Test-ResidentCandidate {
    param(
        [Parameter(Mandatory = $true)][object]$Candidate,
        [Parameter(Mandatory = $true)][int]$DiscoveryTimeoutSec,
        [Parameter(Mandatory = $true)][int]$CompletionTimeoutSec,
        [Parameter(Mandatory = $true)][int]$MaxTokens,
        [Parameter(Mandatory = $true)][bool]$RunCompletion
    )

    $auth = Get-AuthHeaders -ApiKeyEnv $Candidate.api_key_env
    if (-not $auth.ok) {
        return New-CheckResult -Candidate $Candidate -State 'unavailable' -Reason $auth.error
    }

    $root = Normalize-BaseUrl -BaseUrl $Candidate.base_url
    $health = Invoke-JsonGet -Uri "$root/health" -Headers $auth.headers -TimeoutSec $DiscoveryTimeoutSec
    if (-not $health.ok) {
        return New-CheckResult -Candidate $Candidate -State 'unavailable' -Reason "Health check failed: $($health.error)"
    }

    $healthStatus = ([string](Get-PropertyValue -Object $health.value -Name 'status')).ToLowerInvariant()
    if ($healthStatus -and $healthStatus -notin @('ok', 'ready', 'healthy')) {
        return New-CheckResult -Candidate $Candidate -State 'unavailable' -Reason "Health status is '$healthStatus'."
    }

    $modelsResponse = Invoke-JsonGet -Uri "$root/models" -Headers $auth.headers -TimeoutSec $DiscoveryTimeoutSec
    if (-not $modelsResponse.ok) {
        return New-CheckResult -Candidate $Candidate -State 'unavailable' -Reason "Model discovery failed: $($modelsResponse.error)"
    }

    $modelIds = Get-ModelIds -Models $modelsResponse.value
    if ($modelIds -notcontains $Candidate.model) {
        $advertised = if ($modelIds.Count) { $modelIds -join ', ' } else { '<none>' }
        return New-CheckResult -Candidate $Candidate -State 'unavailable' -Reason "Model '$($Candidate.model)' was not advertised. Advertised models: $advertised"
    }

    $capacity = Get-CapacityAssessment -BaseUrl $Candidate.base_url -Headers $auth.headers -TimeoutSec $DiscoveryTimeoutSec
    if ($capacity.state -ne 'ready') {
        return New-CheckResult -Candidate $Candidate -State $capacity.state -Reason $capacity.reason -CapacityState $capacity.state -RuntimeCount $capacity.runtime_count -QueueDepth $capacity.queue_depth
    }

    if (-not $RunCompletion) {
        return New-CheckResult -Candidate $Candidate -State 'ready' -Reason $capacity.reason -CapacityState $capacity.state -RuntimeCount $capacity.runtime_count -QueueDepth $capacity.queue_depth
    }

    $completion = Test-Completion -BaseUrl $Candidate.base_url -Model $Candidate.model -Headers $auth.headers -WireApi $Candidate.wire_api -TimeoutSec $CompletionTimeoutSec -MaxTokens $MaxTokens
    if (-not $completion.ok) {
        return New-CheckResult -Candidate $Candidate -State 'unavailable' -Reason "Capacity was ready, but the completion probe failed: $($completion.error)" -CapacityState $capacity.state -RuntimeCount $capacity.runtime_count -QueueDepth $capacity.queue_depth -CompletionChecked $true
    }

    return New-CheckResult -Candidate $Candidate -State 'ready' -Reason 'Resident Ganglion capacity and completion probe passed.' -CapacityState $capacity.state -RuntimeCount $capacity.runtime_count -QueueDepth $capacity.queue_depth -CompletionChecked $true
}

function Test-GatewayCandidate {
    param(
        [Parameter(Mandatory = $true)][object]$Candidate,
        [Parameter(Mandatory = $true)][int]$DiscoveryTimeoutSec,
        [Parameter(Mandatory = $true)][int]$CompletionTimeoutSec,
        [Parameter(Mandatory = $true)][int]$MaxTokens,
        [Parameter(Mandatory = $true)][bool]$RunCompletion
    )

    if ([string]::IsNullOrWhiteSpace($Candidate.base_url)) {
        return New-CheckResult -Candidate $Candidate -State 'unavailable' -Reason 'External gateway is not configured; set GANGLION_GATEWAY_BASE_URL to opt in.'
    }
    $auth = Get-AuthHeaders -ApiKeyEnv $Candidate.api_key_env
    if (-not $auth.ok) {
        return New-CheckResult -Candidate $Candidate -State 'unavailable' -Reason $auth.error
    }

    $root = Normalize-BaseUrl -BaseUrl $Candidate.base_url
    $modelsResponse = Invoke-JsonGet -Uri "$root/models" -Headers $auth.headers -TimeoutSec $DiscoveryTimeoutSec
    if (-not $modelsResponse.ok) {
        return New-CheckResult -Candidate $Candidate -State 'unavailable' -Reason "Gateway model discovery failed: $($modelsResponse.error)"
    }

    $modelIds = Get-ModelIds -Models $modelsResponse.value
    if ($modelIds -notcontains $Candidate.model) {
        $advertised = if ($modelIds.Count) { $modelIds -join ', ' } else { '<none>' }
        return New-CheckResult -Candidate $Candidate -State 'unavailable' -Reason "Model '$($Candidate.model)' was not advertised. Advertised models: $advertised"
    }

    if (-not $RunCompletion) {
        return New-CheckResult -Candidate $Candidate -State 'ready' -Reason 'Gateway catalog probe passed.'
    }

    $completion = Test-Completion -BaseUrl $Candidate.base_url -Model $Candidate.model -Headers $auth.headers -WireApi $Candidate.wire_api -TimeoutSec $CompletionTimeoutSec -MaxTokens $MaxTokens
    if (-not $completion.ok) {
        return New-CheckResult -Candidate $Candidate -State 'unavailable' -Reason "Gateway catalog passed, but the completion probe failed: $($completion.error)" -CompletionChecked $true
    }

    return New-CheckResult -Candidate $Candidate -State 'ready' -Reason 'Gateway catalog and completion probes passed.' -CompletionChecked $true
}

function New-SkippedResult {
    param(
        [Parameter(Mandatory = $true)][object]$Candidate,
        [Parameter(Mandatory = $true)][string]$Reason
    )
    return New-CheckResult -Candidate $Candidate -State 'skipped' -Reason $Reason
}

$candidates = @(
    [pscustomobject][ordered]@{
        id = 'resident-ganglion'
        order = 1
        scope = 'resident'
        base_url = $LocalBaseUrl
        model = $LocalModel
        api_key_env = $LocalApiKeyEnv
        wire_api = 'ChatCompletions'
        kind = 'resident'
    },
    [pscustomobject][ordered]@{
        id = 'resident-ganglion-continuity'
        order = 2
        scope = 'resident'
        base_url = $ContinuityBaseUrl
        model = $ContinuityModel
        api_key_env = $ContinuityApiKeyEnv
        wire_api = 'ChatCompletions'
        kind = 'resident'
    },
    [pscustomobject][ordered]@{
        id = 'vm108-ganglion-via-litellm'
        order = 3
        scope = 'gateway'
        base_url = $GatewayBaseUrl
        model = $GatewayModel
        api_key_env = $GatewayApiKeyEnv
        wire_api = 'Responses'
        kind = 'gateway'
    }
)

$runCompletion = -not $SkipCompletion.IsPresent
$checks = New-Object System.Collections.Generic.List[object]
$selected = $null
$selectionReason = ''

$primary = Test-ResidentCandidate -Candidate $candidates[0] -DiscoveryTimeoutSec $DiscoveryTimeoutSec -CompletionTimeoutSec $CompletionTimeoutSec -MaxTokens $MaxTokens -RunCompletion $runCompletion
[void]$checks.Add($primary)

if ($primary.state -eq 'ready') {
    $selected = $primary
    $selectionReason = 'The resident Ganglion route is ready; the cascade stopped before checking remote resources.'
    [void]$checks.Add((New-SkippedResult -Candidate $candidates[1] -Reason 'Skipped because the resident primary route was ready.'))
    [void]$checks.Add((New-SkippedResult -Candidate $candidates[2] -Reason 'Skipped because the resident primary route was ready.'))
} else {
    if ($primary.state -eq 'busy') {
        [void]$checks.Add((New-SkippedResult -Candidate $candidates[1] -Reason 'Skipped because the resident Ganglion pool is occupied; the cascade is leaving the resident host.'))
    } else {
        $continuity = Test-ResidentCandidate -Candidate $candidates[1] -DiscoveryTimeoutSec $DiscoveryTimeoutSec -CompletionTimeoutSec $CompletionTimeoutSec -MaxTokens $MaxTokens -RunCompletion $runCompletion
        [void]$checks.Add($continuity)
        if ($continuity.state -eq 'ready') {
            $selected = $continuity
            $selectionReason = 'The primary resident route was unavailable, and the resident continuity route passed.'
        }
    }

    if ($null -eq $selected) {
        $gateway = Test-GatewayCandidate -Candidate $candidates[2] -DiscoveryTimeoutSec $DiscoveryTimeoutSec -CompletionTimeoutSec $CompletionTimeoutSec -MaxTokens $MaxTokens -RunCompletion $runCompletion
        [void]$checks.Add($gateway)
        if ($gateway.state -eq 'ready') {
            $selected = $gateway
            if ($primary.state -eq 'busy') {
                $selectionReason = 'The resident Ganglion route was occupied, so the cascade selected the first ready external gateway.'
            } else {
                $selectionReason = 'Resident routes were unavailable, so the cascade selected the first ready external gateway.'
            }
        }
    }
}

if ($null -eq $selected) {
    $selectionReason = 'No Ganglion route passed the cascade checks; keep the work on the normal native Claude route.'
}

$decision = [ordered]@{
    schema_version = '1'
    policy = 'resident-ganglion-first'
    checked_at = [DateTime]::UtcNow.ToString('o')
    completion_probe = $runCompletion
    selected = $selected
    selection_reason = $selectionReason
    checks = $checks.ToArray()
}

if ($OutputFormat -eq 'Text') {
    if ($null -eq $selected) {
        Write-Output 'Ganglion cascade: no ready route.'
    } else {
        Write-Output ("Ganglion cascade selected: {0} ({1}, {2})" -f $selected.id, $selected.scope, $selected.wire_api)
    }
    Write-Output $selectionReason
    foreach ($check in $checks) {
        Write-Output ("{0}: {1} - {2}" -f $check.id, $check.state, $check.reason)
    }
} else {
    Write-Output ($decision | ConvertTo-Json -Depth 12)
}
