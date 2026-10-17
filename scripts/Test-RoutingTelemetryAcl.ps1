[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$LiteralPath, [string]$ComparePath)
$ErrorActionPreference = 'Stop'
try {
    Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $permittedSids = @($currentSid, 'S-1-5-18', 'S-1-5-32-544') # user, SYSTEM, administrators
    $acl = Get-Acl -LiteralPath $LiteralPath
    $ownerSid = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    $descriptor = [Security.AccessControl.RawSecurityDescriptor]::new($acl.GetSecurityDescriptorBinaryForm(), 0)
    if ($ownerSid -notin $permittedSids -or $null -eq $descriptor.DiscretionaryAcl) {
        @{ private = $false; reason = 'owner_or_null_dacl_not_private' } | ConvertTo-Json -Compress
        exit 0
    }
    foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
        $sid = $rule.IdentityReference.Value
        # OWNER RIGHTS applies only to the already-verified permitted owner.
        if ($sid -eq 'S-1-3-4') { continue }
        # Inherit-only CREATOR OWNER resolves to the creating user's SID on a new
        # file; other unknown principals are conservatively treated as blockers.
        if ($sid -eq 'S-1-3-0' -and ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly)) { continue }
        # Reading metadata/ACLs or SYNCHRONIZE alone does not expose file bytes.
        # Block data access, deletion/replacement, ownership/DACL changes and
        # generic rights for every other principal.
        if ($sid -notin $permittedSids -and ([int64]$rule.FileSystemRights -band 0xF00D0047)) {
            $kind = switch ($sid) { 'S-1-3-0' { 'creator_owner' } 'S-1-3-4' { 'owner_rights' } 'S-1-1-0' { 'everyone' } 'S-1-5-11' { 'authenticated_users' } 'S-1-5-32-545' { 'builtin_users' } default { 'other' } }
            @{ private = $false; reason = 'other_principal_allow_rule'; principal_kind = $kind } | ConvertTo-Json -Compress
            exit 0
        }
    }
    if ($ComparePath) {
        $sections = [Security.AccessControl.AccessControlSections]::Owner -bor [Security.AccessControl.AccessControlSections]::Group -bor [Security.AccessControl.AccessControlSections]::Access
        $originalAcl = Get-Acl -LiteralPath $ComparePath
        if ($acl.GetSecurityDescriptorSddlForm($sections) -ne $originalAcl.GetSecurityDescriptorSddlForm($sections)) {
            @{ private = $false; reason = 'replacement_security_descriptor_would_change' } | ConvertTo-Json -Compress
            exit 0
        }
    }
    @{ private = $true } | ConvertTo-Json -Compress
} catch {
    # No path, identity, ACL, config text or exception details leave the probe.
    @{ private = $false; reason = 'acl_probe_unavailable' } | ConvertTo-Json -Compress
}
