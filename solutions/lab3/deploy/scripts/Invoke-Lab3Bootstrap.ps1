# Run from a reviewed checkout. The request supplies data, never a template path.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $RequestPath,
    [Parameter(Mandatory = $true)]
    [string] $ResultPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (Test-Path variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $false
}

function Get-RequiredString($Object, [string] $Name) {
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property -or $property.Value -isnot [string] -or
        [string]::IsNullOrWhiteSpace($property.Value) -or
        $property.Value -ne $property.Value.Trim()) {
        throw "Missing or invalid string: $Name"
    }
    return $property.Value
}

function Assert-Guid([string] $Value, [string] $Name) {
    $parsed = [guid]::Empty
    if (-not [guid]::TryParseExact($Value, 'D', [ref] $parsed)) {
        throw "Invalid GUID: $Name"
    }
}

function Invoke-AzJson([string[]] $Arguments) {
    # Do not merge stderr into JSON. Keep the original CLI diagnostic and check
    # LASTEXITCODE explicitly on both Windows PowerShell 5.1 and pwsh.
    $text = (& az @Arguments --only-show-errors --output json) -join "`n"
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "az $($Arguments[0]) $($Arguments[1]) failed (exit $exitCode). See CLI error above; no automatic retry."
    }
    if ([string]::IsNullOrWhiteSpace($text)) {
        throw "az $($Arguments[0]) $($Arguments[1]) returned empty JSON."
    }
    if (-not $text.TrimStart().StartsWith('{')) {
        throw "az $($Arguments[0]) $($Arguments[1]) must return a JSON object, not an array or scalar."
    }
    $value = $text | ConvertFrom-Json -ErrorAction Stop
    if ($null -eq $value -or $value -isnot [pscustomobject]) {
        throw "az $($Arguments[0]) $($Arguments[1]) returned an invalid JSON object."
    }
    return $value
}

$templatePath = (Get-Item -LiteralPath (Join-Path $PSScriptRoot '..\infra\main.bicep')).FullName
if (-not [IO.File]::Exists($templatePath)) {
    throw "Reviewed main.bicep not found: $templatePath"
}
$requestFile = Get-Item -LiteralPath $RequestPath
if ($requestFile.PSIsContainer) {
    throw 'RequestPath must be a JSON file.'
}
$resultFile = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ResultPath)
if (Test-Path -LiteralPath $resultFile) {
    throw 'ResultPath already exists. Use a new result file; stale results are not overwritten.'
}
if (-not [IO.Directory]::Exists([IO.Path]::GetDirectoryName($resultFile))) {
    throw 'ResultPath parent directory does not exist.'
}

$requestText = Get-Content -LiteralPath $requestFile.FullName -Raw -Encoding UTF8
if ([string]::IsNullOrWhiteSpace($requestText) -or -not $requestText.TrimStart().StartsWith('{')) {
    throw 'Request must be a non-empty JSON object.'
}
$request = $requestText | ConvertFrom-Json -ErrorAction Stop
$fields = @('subscriptionId', 'resourceGroup', 'environmentName', 'location',
    'accountName', 'projectName', 'studentObjectId')
if ($null -eq $request -or $request -isnot [pscustomobject] -or
    @($request.PSObject.Properties.Name).Count -ne 8 -or
    @($request.PSObject.Properties.Name) -cnotcontains 'schemaVersion' -or
    ($request.schemaVersion -isnot [int] -and $request.schemaVersion -isnot [long]) -or
    $request.schemaVersion -ne 1) {
    throw 'Request must contain schemaVersion 1 and exactly seven required fields.'
}
# az.cmd is parsed by cmd.exe; array splatting does not escape request data.
$resourceNamePatterns = @{
    resourceGroup = '\A[A-Za-z0-9_.-]{1,90}(?<!\.)\z'
    accountName = '\A[A-Za-z0-9][A-Za-z0-9-]{0,62}[A-Za-z0-9]\z'
    projectName = '\A[A-Za-z0-9][A-Za-z0-9_.-]{1,63}\z'
}
foreach ($field in $fields) {
    if (@($request.PSObject.Properties.Name) -cnotcontains $field) {
        throw "Missing request field (names are case-sensitive): $field"
    }
    $value = Get-RequiredString $request $field
    if ($value -match '[/\\\x00-\x1f]' -or $value -match '^<.*>$') {
        throw "Invalid request field: $field"
    }
    if ($resourceNamePatterns.ContainsKey($field) -and
        $value -cnotmatch $resourceNamePatterns[$field]) {
        throw "Invalid request field: $field. See the bootstrap naming restrictions in docs\03-lab3-deploy.md."
    }
}
Assert-Guid $request.subscriptionId 'subscriptionId'
Assert-Guid $request.studentObjectId 'studentObjectId'
if ($request.environmentName.Length -gt 64 -or $request.location -cnotmatch '^[a-z0-9]+$') {
    throw 'Invalid environmentName or location.'
}

$scope = "/subscriptions/$($request.subscriptionId)/resourceGroups/$($request.resourceGroup)"
$projectId = "$scope/providers/Microsoft.CognitiveServices/accounts/$($request.accountName)/projects/$($request.projectName)"
$account = Invoke-AzJson @('account', 'show')
if ((Get-RequiredString $account 'id') -ine $request.subscriptionId) {
    throw 'Active Azure subscription differs from the request. No context was switched.'
}
$executor = Get-RequiredString $account.user 'name'
$executorType = Get-RequiredString $account.user 'type'
$group = Invoke-AzJson @('group', 'show', '--name', $request.resourceGroup,
    '--subscription', $request.subscriptionId)
if ((Get-RequiredString $group 'id') -ine $scope -or
    (Get-RequiredString $group 'location') -ine $request.location) {
    throw 'Resource group scope or location differs from the request.'
}
$project = Invoke-AzJson @('resource', 'show', '--ids', $projectId,
    '--api-version', '2025-04-01-preview', '--subscription', $request.subscriptionId)
if ((Get-RequiredString $project 'id') -ine $projectId) {
    throw 'Existing Foundry project differs from the request.'
}
$projectIdentity = Get-RequiredString $project.identity 'principalId'
Assert-Guid $projectIdentity 'project managed identity'

Write-Host "Executor: $executor ($executorType)"
Write-Host "Scope: $scope"
Write-Host "Environment: $($request.environmentName); location: $($request.location)"
Write-Host "Existing Foundry project: $projectId"
Write-Host "Student objectId (Container Registry Tasks Contributor): $($request.studentObjectId)"
Write-Host "Project managed identity (AcrPull): $projectIdentity"
Write-Host "Reviewed template: $templatePath"
Write-Host 'Creates/updates ACR, its two existing-template role assignments and project connection.'
Write-Host 'Does not grant Owner/UAA/RBAC Administrator or change role assignment conditions.'
if ((Read-Host 'Type BOOTSTRAP to confirm these targets') -cne 'BOOTSTRAP') {
    throw 'Bootstrap cancelled. No ARM deployment was submitted.'
}

$parameterFile = [IO.Path]::GetTempFileName()
$temporaryResult = $null
try {
    # Pass JSON as a file, not interpolated CLI expressions. principalId is
    # deliberately the student, never the administrator running this script.
    $parameters = @{}
    $values = @{
        environmentName = $request.environmentName
        location = $request.location
        principalId = $request.studentObjectId
        principalType = 'User'
        aiServicesAccountName = $request.accountName
        aiProjectName = $request.projectName
    }
    foreach ($name in $values.Keys) {
        $parameters[$name] = @{ value = $values[$name] }
    }
    $parameterDocument = @{
        '$schema' = 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#'
        contentVersion = '1.0.0.0'
        parameters = $parameters
    }
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($parameterFile, ($parameterDocument | ConvertTo-Json -Depth 8), $utf8)
    $deployment = Invoke-AzJson @('deployment', 'group', 'create',
        '--name', 'lab3-bootstrap', '--subscription', $request.subscriptionId,
        '--resource-group', $request.resourceGroup, '--template-file', $templatePath,
        '--parameters', "@$parameterFile")
    $deploymentId = "$scope/providers/Microsoft.Resources/deployments/lab3-bootstrap"
    if ((Get-RequiredString $deployment 'id') -ine $deploymentId -or
        (Get-RequiredString $deployment.properties 'provisioningState') -cne 'Succeeded') {
        throw 'ARM deployment did not succeed at the requested scope.'
    }
    $outputs = @{}
    foreach ($name in @('AZURE_CONTAINER_REGISTRY_ENDPOINT',
        'AZURE_CONTAINER_REGISTRY_RESOURCE_ID', 'AZURE_ACR_CONNECTION_NAME')) {
        $entry = $deployment.properties.outputs.PSObject.Properties[$name]
        if ($null -eq $entry -or
            (Get-RequiredString $entry.Value 'type') -ine 'String') {
            throw "Missing or invalid ARM output: $name"
        }
        $outputs[$name] = Get-RequiredString $entry.Value 'value'
    }
    $registryScope = "$scope/providers/Microsoft.ContainerRegistry/registries/"
    $registryId = $outputs.AZURE_CONTAINER_REGISTRY_RESOURCE_ID
    if (-not $registryId.StartsWith($registryScope, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'ACR output belongs to a different scope.'
    }
    $registryName = $registryId.Substring($registryScope.Length)
    if ($registryName -notmatch '^[a-zA-Z0-9]{5,50}$' -or
        $outputs.AZURE_CONTAINER_REGISTRY_ENDPOINT -ine "$registryName.azurecr.io" -or
        $outputs.AZURE_ACR_CONNECTION_NAME -cnotmatch '^acr-[a-z0-9]{13}$') {
        throw 'Invalid ACR endpoint, resource ID or connection output.'
    }

    $result = @{ schemaVersion = 1; request = $request; outputs = $outputs }
    $temporaryResult = Join-Path ([IO.Path]::GetDirectoryName($resultFile)) ([IO.Path]::GetRandomFileName())
    [IO.File]::WriteAllText($temporaryResult, ($result | ConvertTo-Json -Depth 12), $utf8)
    # A failed CLI/parse/write never publishes a success-shaped result.
    [IO.File]::Move($temporaryResult, $resultFile)
} finally {
    Remove-Item -LiteralPath $parameterFile -ErrorAction Stop
    if ($null -ne $temporaryResult -and [IO.File]::Exists($temporaryResult)) {
        Remove-Item -LiteralPath $temporaryResult -ErrorAction Stop
    }
}
Write-Host "Bootstrap succeeded. Return only this result to the student: $resultFile"
