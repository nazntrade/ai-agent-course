[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [Parameter(Mandatory=$true)][ValidateSet('Scaffold','Specification','Implementation')][string]$Stage,
    [string]$SpecPath,
    [string]$ParentRoot,
    [switch]$AuditExisting,
    [ValidateSet('Standalone','CourseModule')][string]$TemplateType
)
$ErrorActionPreference = 'Stop'
$issues = New-Object System.Collections.Generic.List[object]
$kind = $null
function Add-Issue([string]$Code, [string]$Path, [string]$Message) {
    $issues.Add([pscustomobject]@{code=$Code; path=$Path; message=$Message})
}
function Read-Json([string]$Path) {
    try { return ([IO.File]::ReadAllText($Path) | ConvertFrom-Json) }
    catch { Add-Issue 'INVALID_JSON' $Path 'Expected a valid JSON object.'; return $null }
}
function Check-File([string]$Base, [string]$Relative) {
    $p = Join-Path $Base $Relative
    if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { Add-Issue 'MISSING_FILE' $p 'Required bootstrap file is missing.'; return $false }
    return $true
}
function Check-Filled([string]$Base, [string]$Relative) {
    if (Check-File $Base $Relative) {
        $p=Join-Path $Base $Relative; $text=[IO.File]::ReadAllText($p)
        if ($text -match '(?im)^\s*(?:[-*+]\s+)?TODO\s*:|<(PROJECT_NAME|MODULE_NAME)>' -or [string]::IsNullOrWhiteSpace($text)) {
            Add-Issue 'GOVERNANCE_PLACEHOLDER' $p 'Fill project-specific rules and the selected stack before specification.'
        }
    }
}
# The supported front matter has two-space permission blocks and ordered quoted rules.
# Unknown syntax fails closed instead of assuming a role can perform the action.
function Read-RoleRules([string]$Path,[string]$Action) {
    $rules = New-Object System.Collections.Generic.List[object]
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return @() }
    $text=[IO.File]::ReadAllText($Path)
    $front=[regex]::Match($text,'(?s)^---\r?\n(.*?)\r?\n---')
    if (-not $front.Success) { return @() }
    $active=$false
    foreach($line in ($front.Groups[1].Value -split '\r?\n')) {
        if ($line -match ('^  '+$Action+':\s*(allow|deny|ask)\s*$')) { $rules.Add(@{pattern='*'; value=$Matches[1]}); $active=$false; continue }
        if ($line -match ('^  '+$Action+':\s*$')) { $active=$true; continue }
        if ($active -and $line -match '^    "(.+)":\s*(allow|deny|ask)\s*$') {
            $rules.Add(@{pattern=$Matches[1].Replace('\\','\'); value=$Matches[2]}); continue
        }
        if ($line -match '^  \S') { $active=$false }
    }
    return @($rules.ToArray())
}
function Resolve-Access($GlobalRules,$RoleRules,[string]$Candidate) {
    $value='deny'
    if ($GlobalRules -is [string]) { $value=$GlobalRules }
    elseif ($GlobalRules) {
        foreach($r in $GlobalRules.PSObject.Properties) { if ($Candidate -like $r.Name) { $value=[string]$r.Value } }
    }
    foreach($r in $RoleRules) { if ($Candidate -like $r.pattern) { $value=$r.value } }
    return $value
}
function Check-RoleAccess($Config,[string]$Base,[string]$Relative,[string]$Role,[string]$Action,[string]$Expected) {
    $rolePath=Join-Path $Base ('.opencode/agents/'+$Role+'.md')
    $rules=@(Read-RoleRules $rolePath $Action)
    $candidates=@($Relative)
    if ($Action -eq 'bash') { $candidates=@(('.\'+$Relative.Replace('/','\')), ('./'+$Relative.Replace('\','/'))) }
    $ok=$false
    foreach($c in $candidates) { if ((Resolve-Access $Config.permission.$Action $rules $c) -eq $Expected) { $ok=$true } }
    if (-not $ok) { Add-Issue 'ROLE_PERMISSION' $rolePath ($Role+' needs '+$Expected+' '+$Action+' access to '+$Relative+'.') }
}

function Check-TestRuntime([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
    $blocks=[regex]::Matches([IO.File]::ReadAllText($Path),'(?ms)^```test-runtime-json\s*\r?\n(.*?)^```\s*$')
    if ($blocks.Count -ne 1) { Add-Issue 'TEST_RUNTIME_DECLARATION' $Path 'Exactly one test-runtime-json declaration is required.'; return }
    try { $runtime=$blocks[0].Groups[1].Value | ConvertFrom-Json } catch { Add-Issue 'TEST_RUNTIME_DECLARATION' $Path 'Invalid test runtime JSON.'; return }
    if ($runtime.schemaVersion -ne 1 -or $runtime.applicability -notin @('none','ai')) { Add-Issue 'TEST_RUNTIME_DECLARATION' $Path 'Resolve AI applicability before Specification.'; return }
    if ($runtime.applicability -eq 'none') { return }
    $required=@{profileEnvironment='AI_TEST_MODEL_'; profileRole='chat'; provider='openai-compatible'; absentProfile='explicit-config'; partialProfile='reject'; embeddings='independent'; lifecycle='lease-finally'}
    foreach($field in $required.Keys) { if ($runtime.$field -ne $required[$field]) { Add-Issue 'TEST_RUNTIME_CONTRACT' $Path ('Unsupported or missing test runtime field: '+$field) } }
    $profile=@([Environment]::GetEnvironmentVariables().Keys | Where-Object { $_ -like 'AI_TEST_MODEL_*' })
    if ($profile.Count -eq 0) { return }
    foreach($field in @('KIND','BASE_URL','NAME')) {
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable('AI_TEST_MODEL_'+$field))) { Add-Issue 'TEST_RUNTIME_PROFILE' $Path ('Incomplete selected profile: '+$field) }
    }
    $kind=[Environment]::GetEnvironmentVariable('AI_TEST_MODEL_KIND')
    if ($kind -notin @('local','remote')) { Add-Issue 'TEST_RUNTIME_PROFILE' $Path 'Unsupported selected model kind.' }
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable('AI_TEST_MODEL_API_KEY'))) { Add-Issue 'TEST_RUNTIME_PROFILE' $Path 'Selected profile needs authentication presence.' }
    $baseUri=$null
    $baseValue=[Environment]::GetEnvironmentVariable('AI_TEST_MODEL_BASE_URL')
    $baseValid=[Uri]::TryCreate($baseValue,[UriKind]::Absolute,[ref]$baseUri)
    if (-not $baseValid -or $baseUri.Scheme -notin @('http','https') -or $baseUri.UserInfo -or $baseUri.Query -or $baseUri.Fragment) {
        Add-Issue 'TEST_RUNTIME_PROFILE' $Path 'Invalid selected profile URL: BASE_URL'
        return
    }
    $loopback=$baseUri.DnsSafeHost -in @('127.0.0.1','localhost','::1')
    if (($kind -eq 'local' -and -not $loopback) -or ($kind -eq 'remote' -and $baseUri.Scheme -ne 'https' -and -not $loopback)) {
        Add-Issue 'TEST_RUNTIME_PROFILE' $Path 'Selected profile origin does not match its kind.'
    }
    $leaseValue=[Environment]::GetEnvironmentVariable('AI_TEST_MODEL_LEASE_URL')
    if (-not [string]::IsNullOrWhiteSpace($leaseValue)) {
        $leaseUri=$null
        $modelId=[Environment]::GetEnvironmentVariable('AI_TEST_MODEL_ID')
        $expected='/api/local-models/'+[Uri]::EscapeDataString([string]$modelId)+'/test-leases'
        if (-not [Uri]::TryCreate($leaseValue,[UriKind]::Absolute,[ref]$leaseUri) -or $kind -ne 'local' -or -not $modelId -or
            $leaseUri.Scheme -ne $baseUri.Scheme -or $leaseUri.DnsSafeHost -ne $baseUri.DnsSafeHost -or $leaseUri.Port -ne $baseUri.Port -or
            $leaseUri.AbsolutePath -ne $expected -or $leaseUri.UserInfo -or $leaseUri.Query -or $leaseUri.Fragment) {
            Add-Issue 'TEST_RUNTIME_PROFILE' $Path 'Invalid selected profile lifecycle endpoint.'
        }
    }
    $inherited=[Environment]::GetEnvironmentVariable('AI_TEST_MODEL_LEASE_ID')
    if ($inherited -and ($kind -ne 'local' -or -not $leaseValue -or [Environment]::GetEnvironmentVariable('AI_TEST_MODEL_PARENT_READY') -ne '1')) {
        Add-Issue 'TEST_RUNTIME_PROFILE' $Path 'Invalid inherited test runtime session.'
    }
}

try {
    if (-not [IO.Path]::IsPathRooted($ProjectRoot)) { throw 'ProjectRoot must be absolute.' }
    $root=[IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\','/')
    if (-not (Test-Path -LiteralPath $root -PathType Container)) { Add-Issue 'MISSING_ROOT' $root 'Generate the scaffold first; do not create SPEC files here.' }
    $contractPath=Join-Path $PSScriptRoot 'bootstrap-contract.json'
    $contract=Read-Json $contractPath
    if ($contract -and ($contract.schemaVersion -ne 1 -or $contract.templateVersion -ne 1 -or -not $contract.templates.Standalone -or -not $contract.templates.CourseModule)) {
        Add-Issue 'INVALID_CONTRACT' $contractPath 'Unsupported bootstrap contract.'; $contract=$null
    }
    $markerPath=Join-Path $root '.project-bootstrap.json'
    if ($AuditExisting) {
        if (-not $TemplateType) { Add-Issue 'AUDIT_TYPE_REQUIRED' $root 'AuditExisting requires TemplateType.' }
        $kind=$TemplateType
    } elseif (Test-Path -LiteralPath $markerPath -PathType Leaf) {
        $marker=Read-Json $markerPath
        if ($marker -and $marker.schemaVersion -eq 1 -and $marker.templateVersion -eq 1 -and $marker.templateType -in @('Standalone','CourseModule')) {
            $kind=$marker.templateType
        } else { Add-Issue 'INVALID_MARKER' $markerPath 'Expected schemaVersion 1, templateVersion 1 and a known templateType.' }
    } else { Add-Issue 'MISSING_MARKER' $markerPath 'Run the official generator in an empty destination; never synthesize bootstrap provenance.' }
    if ($kind -and $contract) {
        $definition=$contract.templates.$kind
        foreach($rel in $definition.requiredFiles) { $null=Check-File $root $rel }
        if (-not $AuditExisting) { foreach($rel in $contract.commonFiles) { $null=Check-File $root $rel } }
        $base=$root; $prefix=''
        if ($kind -eq 'CourseModule') {
            foreach($rel in $definition.forbiddenPaths) {
                if (Test-Path -LiteralPath (Join-Path $root $rel)) { Add-Issue 'MODULE_SHADOW_CONFIG' (Join-Path $root $rel) 'CourseModule must inherit course configuration and roles.' }
            }
            if ($ParentRoot) {
                if (-not [IO.Path]::IsPathRooted($ParentRoot)) { throw 'ParentRoot must be absolute.' }
                $base=[IO.Path]::GetFullPath($ParentRoot).TrimEnd('\','/')
            }
            else {
                $base=Split-Path $root -Parent
                while ($base -and -not (Test-Path -LiteralPath (Join-Path $base 'opencode.json') -PathType Leaf)) { $base=Split-Path $base -Parent }
            }
            if (-not $base -or -not $root.StartsWith($base+'\',[StringComparison]::OrdinalIgnoreCase)) {
                Add-Issue 'PARENT_ROOT' $root 'A containing course root with inherited configuration is required.'; $base=$null
            } else {
                $prefix=$root.Substring($base.Length+1).Replace('\','/')+'/'
                foreach($rel in $definition.inheritedFiles) { $null=Check-File $base $rel }
            }
        }
        if ($Stage -ne 'Scaffold') {
            foreach($rel in $definition.governanceFiles) { Check-Filled $root $rel }
            $runtimeFile='PROJECT_RULES.md'
            if ($kind -eq 'CourseModule') { $runtimeFile='MODULE_RULES.md' }
            Check-TestRuntime (Join-Path $root $runtimeFile)
            $stackFile='STACK_PROFILE.md'; $heading='Selected profile'
            if ($kind -eq 'CourseModule') { $stackFile='MODULE_RULES.md'; $heading='Стек и версии' }
            $stackPath=Join-Path $root $stackFile
            if (Test-Path -LiteralPath $stackPath -PathType Leaf) {
                $stack=[regex]::Match([IO.File]::ReadAllText($stackPath),'(?ms)^##[^\r\n]*'+[regex]::Escape($heading)+'[^\r\n]*\r?\n(.*?)(?=^##|\z)')
                if (-not $stack.Success -or [string]::IsNullOrWhiteSpace($stack.Groups[1].Value)) {
                    Add-Issue 'STACK_SELECTION' $stackPath 'Declare the selected stack in its dedicated section.'
                }
            }
            if ($base -and (Test-Path -LiteralPath (Join-Path $base 'opencode.json') -PathType Leaf)) {
                $config=Read-Json (Join-Path $base 'opencode.json')
                if ($config) {
                    foreach($rel in $definition.governanceFiles) {
                        Check-RoleAccess $config $base ($prefix+$rel) 'configurator' 'edit' 'ask'
                        Check-RoleAccess $config $base ($prefix+$rel) 'developer' 'edit' 'deny'
                    }
                    foreach($bat in $contract.launchFiles) {
                        Check-RoleAccess $config $base ($prefix+$bat) 'configurator' 'edit' 'ask'
                        Check-RoleAccess $config $base ($prefix+$bat) 'developer' 'edit' 'deny'
                        foreach($role in @('developer','tester')) { Check-RoleAccess $config $base ($prefix+$bat) $role 'bash' 'allow' }
                    }
                }
            }
        }
        if ($Stage -eq 'Implementation') {
            foreach($bat in $contract.launchFiles) {
                if (Test-Path -LiteralPath (Join-Path $root $bat) -PathType Leaf) {
                    if ([IO.File]::ReadAllText((Join-Path $root $bat)) -match 'CONFIGURE_ME') { Add-Issue 'LAUNCH_PLACEHOLDER' (Join-Path $root $bat) 'Configure the launch script from the approved PLAN before implementation.' }
                }
            }
            if (-not $SpecPath -or [IO.Path]::IsPathRooted($SpecPath) -or $SpecPath.Replace('\','/') -notmatch '^docs/specs/[^/]+$') {
                Add-Issue 'SPEC_PATH' $root 'Supply an exact relative docs/specs/name directory.'
            } else {
                foreach($name in @('SPEC.md','PLAN.md','ACCEPTANCE.md')) {
                    $rel=$SpecPath+'/'+$name
                    if (Check-File $root $rel) {
                        if ([string]::IsNullOrWhiteSpace([IO.File]::ReadAllText((Join-Path $root $rel)))) { Add-Issue 'EMPTY_SPEC' (Join-Path $root $rel) 'The specification document is empty.' }
                    }
                }
            }
        }
    }
} catch { Add-Issue 'INVALID_INPUT' $ProjectRoot $_.Exception.Message }
$result=[ordered]@{schemaVersion=1; templateType=$kind; stage=$Stage; ready=($issues.Count -eq 0); issues=@($issues.ToArray())}
if ($AuditExisting) { $result.auditOnly=$true }
$result | ConvertTo-Json -Depth 8 -Compress
if ($issues.Count -eq 0) { exit 0 } else { exit 2 }
