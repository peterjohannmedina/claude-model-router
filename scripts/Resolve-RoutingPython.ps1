[CmdletBinding()]
param([string]$PythonPath)
$ErrorActionPreference = 'Stop'
if ($PythonPath) { $routingCandidates = @($PythonPath) } else {
    $routingUserRoot = [Environment]::GetFolderPath('UserProfile')
    $routingCandidates = @($env:CLAUDE_ROUTING_PYTHON)
    foreach ($routingName in @('python3', 'python')) {
        $routingCommand = Get-Command $routingName -ErrorAction SilentlyContinue
        if ($routingCommand -and $routingCommand.Source -notmatch 'WindowsApps') { $routingCandidates += $routingCommand.Source }
    }
}
foreach ($routingCandidate in $routingCandidates) {
    if ($routingCandidate -and (Test-Path -LiteralPath $routingCandidate -PathType Leaf)) {
        & $routingCandidate -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { (Resolve-Path -LiteralPath $routingCandidate).Path; return }
    }
}
throw 'Python 3.11+ required; set CLAUDE_ROUTING_PYTHON to an existing interpreter. No runtime was installed.'
