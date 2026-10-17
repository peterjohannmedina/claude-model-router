# Test-only helper: never accepts a path outside this test suite's temp root.
[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$LiteralPath, [switch]$GrantEveryoneRead)
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
$fixture = (Resolve-Path -LiteralPath $LiteralPath).Path
$allowedRoot = [IO.Path]::GetFullPath((Split-Path -Parent (Split-Path -Parent $PSScriptRoot)))
if ((Split-Path -Parent $fixture) -ne $allowedRoot -or (Split-Path -Leaf $fixture) -notlike 'routing-otel-setup-*') {
    throw 'Refusing ACL fixture mutation outside the named synthetic directory.'
}
if ($GrantEveryoneRead) {
    $config = Join-Path $fixture 'config.toml'
    $acl = [IO.File]::GetAccessControl($config, [Security.AccessControl.AccessControlSections]::Access)
    $everyone = [Security.Principal.SecurityIdentifier]::new('S-1-1-0')
    $rule = [Security.AccessControl.FileSystemAccessRule]::new($everyone, [Security.AccessControl.FileSystemRights]::Read, [Security.AccessControl.AccessControlType]::Allow)
    $acl.AddAccessRule($rule)
    [IO.File]::SetAccessControl($config, $acl)
} else {
    $acl = [IO.Directory]::GetAccessControl($fixture, [Security.AccessControl.AccessControlSections]::Access)
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($existing in $acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier])) {
        $acl.RemoveAccessRuleSpecific($existing)
    }
    $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid, [Security.AccessControl.FileSystemRights]::FullControl,
        ([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit),
        [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow)
    $acl.AddAccessRule($rule)
    [IO.Directory]::SetAccessControl($fixture, $acl)
}
