param(
    [string]$OutputDirectory = "dist",
    [string]$OutputFilename = "",
    [string]$BuildId = "",
    [string]$RuntimeProfileId = "",
    [switch]$PassiveCapture,
    [switch]$OfficialRelease,
    [switch]$ProtectedRelease,
    [string]$CapabilitySigningKeyId = "",
    [string]$CapabilityPublicKey = "",
    [string]$SigningCertificateThumbprint = "",
    [string[]]$TrustedPublisherThumbprints = @(),
    [string]$TimestampServer = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"
$PacketCapture = "windows_raw"

function Write-Utf8NoBom([string]$Path, [string]$Value) {
    $Encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Value, $Encoding)
}

function Normalize-Thumbprint([string]$Value) {
    return (($Value -replace '\s+', '').ToUpperInvariant())
}

function Find-CodeSigningCertificate([string]$Thumbprint) {
    $Normalized = Normalize-Thumbprint $Thumbprint
    $Candidates = @(
        Get-ChildItem -Path Cert:\CurrentUser\My, Cert:\LocalMachine\My `
            -ErrorAction SilentlyContinue |
            Where-Object {
                (Normalize-Thumbprint $_.Thumbprint) -eq $Normalized -and
                $_.HasPrivateKey -and
                $_.NotAfter -gt (Get-Date) -and
                ($_.EnhancedKeyUsageList.ObjectId.Value -contains '1.3.6.1.5.5.7.3.3')
            }
    )
    if ($Candidates.Count -ne 1) {
        throw "Exactly one usable code-signing certificate must match $Normalized"
    }
    return $Candidates[0]
}

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectDir ".venv-build310\Scripts\python.exe"
$CapstoneDll = Join-Path $ProjectDir ".venv-build310\Lib\site-packages\capstone\lib\capstone.dll"
$ZstdRestoreDll = Join-Path $ProjectDir "npcap_zstd_restore.dll"
$ZstdLicensePath = Join-Path $ProjectDir "third_party_licenses\zstandard-BSD.txt"
$WinDivertRoot = Join-Path $ProjectDir "third_party\windivert"
$WinDivertDll = Join-Path $WinDivertRoot "x64\WinDivert.dll"
$WinDivertDriver = Join-Path $WinDivertRoot "x64\WinDivert64.sys"
$WinDivertLicense = Join-Path $WinDivertRoot "LICENSE"
$WinDivertVersionFile = Join-Path $WinDivertRoot "VERSION"
$ExpectedWinDivertDllSha256 = "C1E060EE19444A259B2162F8AF0F3FE8C4428A1C6F694DCE20DE194AC8D7D9A2"
$ExpectedWinDivertDriverSha256 = "8DA085332782708D8767BCACE5327A6EC7283C17CFB85E40B03CD2323A90DDC2"
$SourcePath = Join-Path $ProjectDir "dps_meter.pyw"
$DevelopmentRuntimeProfilePath = Join-Path $ProjectDir "runtime-profile.dev.json"
if ($PacketCapture -eq "windows_raw") {
    foreach ($RequiredWinDivertFile in @(
        $WinDivertDll,
        $WinDivertDriver,
        $WinDivertLicense,
        $WinDivertVersionFile
    )) {
        if (-not (Test-Path -LiteralPath $RequiredWinDivertFile -PathType Leaf)) {
            throw "Bundled IPv6 capture resource was not found: $RequiredWinDivertFile"
        }
    }
    $WinDivertVersion = (
        Get-Content -LiteralPath $WinDivertVersionFile -Raw -Encoding ASCII
    ).Trim()
    if ($WinDivertVersion -ne "2.2.2") {
        throw "Bundled WinDivert version must be exactly 2.2.2"
    }
    $WinDivertDllHash = (
        Get-FileHash -LiteralPath $WinDivertDll -Algorithm SHA256
    ).Hash.ToUpperInvariant()
    $WinDivertDriverHash = (
        Get-FileHash -LiteralPath $WinDivertDriver -Algorithm SHA256
    ).Hash.ToUpperInvariant()
    if ($WinDivertDllHash -ne $ExpectedWinDivertDllSha256) {
        throw "Bundled WinDivert.dll hash is not the reviewed official binary"
    }
    if ($WinDivertDriverHash -ne $ExpectedWinDivertDriverSha256) {
        throw "Bundled WinDivert64.sys hash is not the reviewed official binary"
    }
    $WinDivertDriverSignature = Get-AuthenticodeSignature -LiteralPath $WinDivertDriver
    if ($WinDivertDriverSignature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
        throw "Bundled WinDivert64.sys must have a valid Authenticode signature"
    }
}
$ProductName = "$([char]0x53E8)$([char]0x53E8)$([char]0x8BE1)$([char]0x79D8)$([char]0x52A9)$([char]0x624B)"
$DisplayName = $ProductName
$Description = "$DisplayName $([char]0x6218)$([char]0x6597)$([char]0x6570)$([char]0x636E)$([char]0x7EDF)$([char]0x8BA1)"
$ReleaseNotesLabel = "$([char]0x66F4)$([char]0x65B0)$([char]0x65E5)$([char]0x5FD7)"
$SourceText = Get-Content -LiteralPath $SourcePath -Raw -Encoding UTF8
$VersionMatch = [regex]::Match(
    $SourceText,
    '(?m)^\s*APP_VERSION\s*=\s*"(?<version>\d+(?:\.\d+){2,3}[a-z]?)"\s*$'
)
$ClientBuildMatch = [regex]::Match(
    $SourceText,
    '(?m)^\s*CLIENT_BUILD\s*=\s*"(?<build>[^"\r\n]{1,64})"\s*$'
)
if (-not $VersionMatch.Success -or -not $ClientBuildMatch.Success) {
    throw "APP_VERSION or CLIENT_BUILD was not found in $SourcePath"
}
$SourceAppVersion = $VersionMatch.Groups["version"].Value
$AppVersion = $SourceAppVersion
$ClientBuild = $ClientBuildMatch.Groups["build"].Value
$DisplayVersionMatch = [regex]::Match(
    $AppVersion,
    '^(?<numeric>\d+(?:\.\d+){2,3})(?<suffix>[a-z]?)$'
)
$ClientBuildVersionMatch = [regex]::Match(
    $ClientBuild,
    '^(?<version>\d+(?:\.\d+){2,3})\+(?<revision>\d+(?:\.\d+)*)$'
)
if (-not $DisplayVersionMatch.Success -or -not $ClientBuildVersionMatch.Success) {
    throw "APP_VERSION or CLIENT_BUILD has an invalid release version format"
}
$NumericAppVersion = $DisplayVersionMatch.Groups["numeric"].Value
$UpdateVersion = $ClientBuildVersionMatch.Groups["version"].Value
if ($NumericAppVersion -ne $UpdateVersion) {
    throw "APP_VERSION and CLIENT_BUILD must use the same numeric release version"
}
$ReleaseNotesSourcePath = Join-Path $ProjectDir "release-notes-v$AppVersion.txt"
$ReleaseNotes = if (Test-Path -LiteralPath $ReleaseNotesSourcePath -PathType Leaf) {
    (Get-Content -LiteralPath $ReleaseNotesSourcePath -Raw -Encoding UTF8).Trim()
} else {
    ""
}
$FileVersionParts = @($NumericAppVersion.Split('.') | ForEach-Object { [int]$_ })
while ($FileVersionParts.Count -lt 4) {
    $FileVersionParts += 0
}
$DisplayVersionSuffix = $DisplayVersionMatch.Groups["suffix"].Value
if ($DisplayVersionSuffix) {
    if (($NumericAppVersion.Split('.')).Count -eq 4) {
        throw "APP_VERSION cannot combine a four-part version with a letter suffix"
    }
    $FileVersionParts[3] = (
        ([int][char]$DisplayVersionSuffix) - ([int][char]'a') + 1
    )
}
$FileVersion = $FileVersionParts -join '.'
$OutputName = if ($OutputFilename) {
    [System.IO.Path]::GetFileName($OutputFilename)
} else {
    "$ProductName-v$AppVersion.exe"
}
if (-not $OutputName.EndsWith('.exe', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "OutputFilename must end with .exe"
}
$OutputDirectoryPath = if ([System.IO.Path]::IsPathRooted($OutputDirectory)) {
    [System.IO.Path]::GetFullPath($OutputDirectory)
} else {
    Join-Path $ProjectDir $OutputDirectory
}

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python was not found: $Python"
}
if (-not (Test-Path -LiteralPath $CapstoneDll -PathType Leaf)) {
    throw "Capstone DLL was not found: $CapstoneDll"
}

$NormalizedBuildId = ($BuildId -replace '[-\s]+', '').ToLowerInvariant()
if (-not $NormalizedBuildId) {
    $NormalizedBuildId = [guid]::NewGuid().ToString('N').ToLowerInvariant()
}
if ($NormalizedBuildId -notmatch '^[0-9a-f]{32}$') {
    throw "BuildId must contain exactly 32 hexadecimal characters"
}

$RuntimeProfileId = $RuntimeProfileId.Trim()
if (-not $RuntimeProfileId) {
    if (Test-Path -LiteralPath $DevelopmentRuntimeProfilePath -PathType Leaf) {
        $DevelopmentRuntimeProfile = Get-Content -LiteralPath `
            $DevelopmentRuntimeProfilePath -Raw -Encoding UTF8 | ConvertFrom-Json
        $RuntimeProfileId = [string]$DevelopmentRuntimeProfile.profile_id
    }
}
if ($RuntimeProfileId -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{7,63}$') {
    throw "RuntimeProfileId is missing or invalid"
}

$SigningCertificate = $null
$SigningThumbprint = Normalize-Thumbprint $SigningCertificateThumbprint
$TrustedThumbprints = @(
    $TrustedPublisherThumbprints |
        ForEach-Object { Normalize-Thumbprint $_ } |
        Where-Object { $_ } |
        Select-Object -Unique
)
if ($OfficialRelease) {
    if ($SigningThumbprint -notmatch '^[0-9A-F]{40}([0-9A-F]{24})?$') {
        throw "OfficialRelease requires a valid SigningCertificateThumbprint"
    }
    $SigningCertificate = Find-CodeSigningCertificate $SigningThumbprint
    $TrustedThumbprints = @($SigningThumbprint) + @(
        $TrustedThumbprints | Where-Object { $_ -ne $SigningThumbprint }
    )
}
foreach ($Thumbprint in $TrustedThumbprints) {
    if ($Thumbprint -notmatch '^[0-9A-F]{40}([0-9A-F]{24})?$') {
        throw "Trusted publisher thumbprint is invalid: $Thumbprint"
    }
}

$CapabilitySigningKeyId = $CapabilitySigningKeyId.Trim()
$CapabilityPublicKey = $CapabilityPublicKey.Trim()
$CapabilityPublicKeys = [ordered]@{}
if ($ProtectedRelease) {
    if ($CapabilitySigningKeyId -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{7,63}$') {
        throw "ProtectedRelease requires a valid CapabilitySigningKeyId"
    }
    try {
        $DecodedCapabilityPublicKey = [Convert]::FromBase64String($CapabilityPublicKey)
    }
    catch {
        throw "ProtectedRelease requires a base64 CapabilityPublicKey"
    }
    if ($DecodedCapabilityPublicKey.Length -ne 32) {
        throw "CapabilityPublicKey must contain exactly 32 Ed25519 key bytes"
    }
    $CapabilityPublicKeys[$CapabilitySigningKeyId] = $CapabilityPublicKey
}
elseif ($CapabilitySigningKeyId -or $CapabilityPublicKey) {
    throw "Capability signing parameters require ProtectedRelease"
}

$TemporaryDirectory = Join-Path (
    [System.IO.Path]::GetTempPath()
) "gmzz-dps-build-$NormalizedBuildId"
New-Item -ItemType Directory -Path $TemporaryDirectory -Force | Out-Null

# The development profile is intentionally the only repository file that may
# contain concrete hook entry points.  A regression that writes them back into
# a compiled client module must fail before Nuitka starts.
$CompiledCaptureSourceNames = @(
    "dps_meter.pyw",
    "capture_backend.py",
    "runtime_capability.py",
    "pvp_tracker.py",
    "pvp_records.py",
    "pvp_backend_ui.py"
)
$CompiledCaptureSourceNames += @(
    # The protocol decoder retains its historical module names internally;
    # the only bundled transport is the receive-only Windows implementation.
    "npcap_capture_process.py",
    "npcap_protocol.py",
    "npcap_key_state.py",
    "npcap_zstd_state.py",
    "npcap_rc4_decode.py",
    "npcap_shadow_capture.py",
    "npcap_receiver.py",
    "windows_raw_receiver.py",
    "windivert_receiver.py",
    "windows_hybrid_receiver.py",
    "windows_capture_process.py",
    "npcap_entity_metadata.py",
    "runtime_metadata.py",
    "npcap_parser_adapter.py",
    "npcap_method_tables.py",
    "npcap_runtime.py",
    "startup_bootstrap.py",
    "passive_transport.py",
    "proc_inspect.py"
)
$CompiledCaptureSources = $CompiledCaptureSourceNames |
    ForEach-Object { Join-Path $ProjectDir $_ }
$ForbiddenCaptureLiterals = @(
    "0x0997DBD0",
    "0x09A5AD40",
    "0x06733B00",
    "0x0672D7C0",
    "0x068D6550",
    "0x068D5A02",
    "0x068D78F0",
    "0x068D70A0",
    "0x0EC0A080",
    "0x0EC0A094",
    "ReqCommonCombatStatisticsByTeam"
)
foreach ($CaptureSource in $CompiledCaptureSources) {
    $CaptureText = Get-Content -LiteralPath $CaptureSource -Raw -Encoding UTF8
    foreach ($Literal in $ForbiddenCaptureLiterals) {
        if ($CaptureText.Contains($Literal)) {
            throw "Sensitive runtime literal was found in compiled source: $CaptureSource"
        }
    }
}

$ResourceFiles = @()
$AssetRoot = Join-Path $ProjectDir "assets"
Get-ChildItem -LiteralPath $AssetRoot -Recurse -File |
    Where-Object { $_.Extension -in @('.png', '.ico') } |
    Sort-Object FullName |
    ForEach-Object {
        $Relative = $_.FullName.Substring($ProjectDir.Length).TrimStart('\')
        $ResourceFiles += [pscustomobject]@{
            Source = $_.FullName
            Target = $Relative.Replace('\', '/')
        }
    }
$SourceIconManifest = Get-Content -LiteralPath (
    Join-Path $AssetRoot "icon_sources.json"
) -Raw -Encoding UTF8 | ConvertFrom-Json
$SanitizedIconManifest = [ordered]@{
    schema_version = 1
    professions = [ordered]@{}
    skills = [ordered]@{}
    skill_icon_aliases = [ordered]@{}
}
foreach ($SectionName in @('professions', 'skills', 'skill_icon_aliases')) {
    $Section = $SourceIconManifest.$SectionName
    if ($null -eq $Section) {
        continue
    }
    foreach ($Property in $Section.PSObject.Properties) {
        $ProfessionIds = @($Property.Value.profession_ids) |
            ForEach-Object { [int]$_ } |
            Sort-Object -Unique
        if ($SectionName -eq 'professions') {
            $ProfessionIds = @([int]$Property.Name)
        }
        $SanitizedIconManifest[$SectionName][$Property.Name] = [ordered]@{
            profession_ids = @($ProfessionIds)
        }
    }
}
$SanitizedIconManifestPath = Join-Path $TemporaryDirectory "icon_sources.json"
Write-Utf8NoBom $SanitizedIconManifestPath (
    $SanitizedIconManifest | ConvertTo-Json -Depth 6
)
$ResourceFiles += [pscustomobject]@{
    Source = $SanitizedIconManifestPath
    Target = "assets/icon_sources.json"
}
$SourceBossCatalog = Get-Content -LiteralPath (
    Join-Path $AssetRoot "bosses\boss_icon_sources.json"
) -Raw -Encoding UTF8 | ConvertFrom-Json
$SanitizedBossCatalog = [ordered]@{
    schema_version = [int]$SourceBossCatalog.schema_version
    dungeons = [ordered]@{}
    bosses = [ordered]@{}
    client_stage_templates = [ordered]@{}
    stages = [ordered]@{}
    stage_name_index = [ordered]@{}
}
foreach ($SectionName in @(
    'dungeons',
    'bosses',
    'client_stage_templates',
    'stages',
    'stage_name_index'
)) {
    $Section = $SourceBossCatalog.$SectionName
    if ($null -eq $Section) {
        continue
    }
    foreach ($Property in $Section.PSObject.Properties) {
        $SanitizedBossCatalog[$SectionName][$Property.Name] = $Property.Value
    }
}
$SanitizedBossCatalogPath = Join-Path $TemporaryDirectory "boss_icon_sources.json"
Write-Utf8NoBom $SanitizedBossCatalogPath (
    $SanitizedBossCatalog | ConvertTo-Json -Depth 8
)
$ResourceFiles += [pscustomobject]@{
    Source = $SanitizedBossCatalogPath
    Target = "assets/bosses/boss_icon_sources.json"
}
$ResourceFiles += [pscustomobject]@{
    Source = Join-Path $AssetRoot "bosses\hud\manifest.json"
    Target = "assets/bosses/hud/manifest.json"
}
$PvpAppearanceManifest = Join-Path $AssetRoot "pvp\appearance_manifest.json"
if (Test-Path -LiteralPath $PvpAppearanceManifest) {
    $ResourceFiles += [pscustomobject]@{
        Source = $PvpAppearanceManifest
        Target = "assets/pvp/appearance_manifest.json"
    }
}
foreach ($Name in @(
    "skill_names.json",
    "skill_metadata.json",
    "monster_metadata.json",
    "boss_enrage_config.json",
    "boss_allowlist.txt",
    "cacert.pem"
)) {
    $ResourceFiles += [pscustomobject]@{
        Source = Join-Path $ProjectDir $Name
        Target = $Name
    }
}
# Development builds need the explicit local profile. Protected builds receive
# the same data only through short-lived, server-signed runtime capabilities.
# Authenticode signing remains an independent release decision.
if (-not $ProtectedRelease) {
    $ResourceFiles += [pscustomobject]@{
        Source = $DevelopmentRuntimeProfilePath
        Target = "runtime-profile.dev.json"
    }
}
$CaptureVariantPath = Join-Path $TemporaryDirectory "_capture_variant.json"
$CaptureVariant = [ordered]@{
    schema_version = 1
    backend = "windows_raw"
    display_version = $AppVersion
    data_directory = "GMZZDpsMeter"
    mutex_name = ""
    tray_class_prefix = ""
    packet_capture = "windows_raw"
    passive_capture = $true
    native_ipv4_capture = "windows_raw_socket"
    native_ipv6_capture = "windivert_sniff_receive_only"
    packet_transmit_functions_loaded = $false
    server_requests_added = 2
    game_process_access = "query_read_write_bounded_rpc_hooks"
}
Write-Utf8NoBom $CaptureVariantPath (
    $CaptureVariant | ConvertTo-Json -Depth 5
)
$ResourceFiles += [pscustomobject]@{
    Source = $CaptureVariantPath
    Target = "_capture_variant.json"
}
$ResourceFiles += [pscustomobject]@{
    Source = $ZstdLicensePath
    Target = "third_party_licenses/zstandard-BSD.txt"
}
$ResourceFiles += [pscustomobject]@{
    Source = $ZstdRestoreDll
    Target = "npcap_zstd_restore.dll"
}
if ($PacketCapture -eq "windows_raw") {
    $ResourceFiles += [pscustomobject]@{
        Source = $WinDivertDll
        Target = "windivert/WinDivert.dll"
    }
    $ResourceFiles += [pscustomobject]@{
        Source = $WinDivertDriver
        Target = "windivert/WinDivert64.sys"
    }
    $ResourceFiles += [pscustomobject]@{
        Source = $WinDivertLicense
        Target = "third_party_licenses/WinDivert-LGPL-GPL.txt"
    }
}
$ResourceFiles += [pscustomobject]@{
    Source = $CapstoneDll
    Target = "capstone/lib/capstone.dll"
}

$ResourceHashes = [ordered]@{}
foreach ($Resource in $ResourceFiles) {
    if (-not (Test-Path -LiteralPath $Resource.Source -PathType Leaf)) {
        throw "Release resource was not found: $($Resource.Source)"
    }
    $ResourceHashes[$Resource.Target] = (
        Get-FileHash -LiteralPath $Resource.Source -Algorithm SHA256
    ).Hash.ToLowerInvariant()
}

$IdentityPath = Join-Path $TemporaryDirectory "_release_identity.json"
$Identity = [ordered]@{
    schema_version = 2
    product = $DisplayName
    version = $AppVersion
    client_build = $ClientBuild
    build_id = $NormalizedBuildId
    runtime_profile_id = $RuntimeProfileId
    official = [bool]$OfficialRelease
    protected = [bool]$ProtectedRelease
    publisher_thumbprints = @($TrustedThumbprints)
    capability_public_keys = $CapabilityPublicKeys
    resource_hashes = $ResourceHashes
    generated_at_utc = [DateTime]::UtcNow.ToString('o')
}
Write-Utf8NoBom $IdentityPath ($Identity | ConvertTo-Json -Depth 8)

New-Item -ItemType Directory -Path $OutputDirectoryPath -Force | Out-Null
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:VSLANG = "1033"
$env:CL = "/utf-8"

Push-Location $ProjectDir
try {
    $NuitkaArguments = @(
        "-m", "nuitka",
        "--mode=onefile",
        "--windows-console-mode=disable",
        "--enable-plugin=tk-inter",
        "--assume-yes-for-downloads",
        "--jobs=4",
        "--remove-output",
        "--deployment",
        "--python-flag=no_docstrings",
        "--python-flag=no_asserts",
        "--python-flag=isolated",
        "--python-flag=safe_path",
        "--file-reference-choice=runtime",
        "--lto=yes",
        "--no-pyi-file",
        "--nofollow-import-to=pytest",
        "--nofollow-import-to=unittest",
        "--nofollow-import-to=*.tests",
        "--report=$TemporaryDirectory\nuitka-report.xml",
        "--output-dir=$OutputDirectoryPath",
        "--output-filename=$OutputName",
        "--windows-icon-from-ico=assets/app_icon.ico",
        "--include-package=capstone",
        "--include-module=main_hud",
        "--include-module=main_hud_artwork",
        "--include-module=pvp_tracker",
        "--include-module=pvp_records",
        "--include-module=pvp_backend_ui",
        "--include-module=equipment_profiles",
        "--include-module=ksbc2_skill_names",
        "--include-module=profile_upload",
        "--include-module=resumable_update",
        "--include-module=update_cdn",
        "--include-data-files=$CapstoneDll=capstone/lib/capstone.dll",
        "--include-data-files=assets/*.png=assets/",
        "--include-data-files=assets/professions/*.png=assets/professions/",
        "--include-data-files=assets/skills/*.png=assets/skills/",
        "--include-data-files=assets/equipment/*.png=assets/equipment/",
        "--include-data-files=assets/equipment/manifest.json=assets/equipment/manifest.json",
        "--include-data-files=assets/bosses/*.png=assets/bosses/",
        "--include-data-files=assets/bosses/hud/*.png=assets/bosses/hud/",
        "--include-data-files=assets/bosses/hud/manifest.json=assets/bosses/hud/manifest.json",
        "--include-data-files=assets/pvp/avatars/*.png=assets/pvp/avatars/",
        "--include-data-files=assets/pvp/frames/*.png=assets/pvp/frames/",
        "--include-data-files=assets/pvp/appearance_manifest.json=assets/pvp/appearance_manifest.json",
        "--include-data-files=assets/app_icon.ico=assets/app_icon.ico",
        "--include-data-files=$SanitizedIconManifestPath=assets/icon_sources.json",
        "--include-data-files=$SanitizedBossCatalogPath=assets/bosses/boss_icon_sources.json",
        "--include-data-files=skill_names.json=skill_names.json",
        "--include-data-files=skill_metadata.json=skill_metadata.json",
        "--include-data-files=monster_metadata.json=monster_metadata.json",
        "--include-data-files=map_catalog_20260918.csv=map_catalog_20260918.csv",
        "--include-data-files=boss_enrage_config.json=boss_enrage_config.json",
        "--include-data-files=boss_allowlist.txt=boss_allowlist.txt",
        "--include-data-files=cacert.pem=cacert.pem",
        "--include-data-files=$IdentityPath=_release_identity.json",
        "--file-version=$FileVersion",
        "--product-version=$FileVersion",
        "--product-name=$DisplayName",
        "--file-description=$Description",
        "--copyright=$ProductName",
        "dps_meter.pyw"
    )
    $BackendArguments = @(
            "--include-module=npcap_capture_process",
            "--include-module=npcap_protocol",
            "--include-module=npcap_key_state",
            "--include-module=npcap_zstd_state",
            "--include-module=npcap_rc4_decode",
            "--include-module=npcap_shadow_capture",
            "--include-module=npcap_receiver",
            "--include-module=windows_raw_receiver",
            "--include-module=windivert_receiver",
            "--include-module=windows_hybrid_receiver",
            "--include-module=windows_capture_process",
            "--include-module=npcap_bootstrap",
            "--include-module=npcap_wire_entities",
            "--include-module=npcap_entity_metadata",
            "--include-module=runtime_metadata",
            "--include-module=combat_statistics",
            "--include-module=encounter_tracker",
            "--include-module=encounter_repository",
            "--include-module=npcap_parser_adapter",
            "--include-module=npcap_method_tables",
            "--include-module=npcap_runtime",
            "--include-module=network_capture",
            "--include-module=inline_capture",
            "--include-module=team_stats_request_hook",
            "--include-module=startup_bootstrap",
            "--include-module=passive_transport",
            "--include-package=msgpack",
            "--include-package=zstandard",
            "--nofollow-import-to=capture_process",
            "--nofollow-import-to=damage_hook"
        )
    $NuitkaArguments = @(
        $NuitkaArguments[0..($NuitkaArguments.Count - 2)]
        $BackendArguments
        $NuitkaArguments[-1]
    )
    $NuitkaArguments = @(
        $NuitkaArguments[0..($NuitkaArguments.Count - 2)]
        "--include-data-files=$CaptureVariantPath=_capture_variant.json"
        "--include-data-files=$ZstdRestoreDll=npcap_zstd_restore.dll"
        "--include-data-files=$ZstdLicensePath=third_party_licenses/zstandard-BSD.txt"
        $NuitkaArguments[-1]
    )
    if ($PacketCapture -eq "windows_raw") {
        $NuitkaArguments = @(
            $NuitkaArguments[0..($NuitkaArguments.Count - 2)]
            "--include-data-files=$WinDivertDll=windivert/WinDivert.dll"
            "--include-data-files=$WinDivertDriver=windivert/WinDivert64.sys"
            "--include-data-files=$WinDivertLicense=third_party_licenses/WinDivert-LGPL-GPL.txt"
            $NuitkaArguments[-1]
        )
    }
    if (-not $ProtectedRelease) {
        $NuitkaArguments = @(
            $NuitkaArguments[0..($NuitkaArguments.Count - 2)]
            "--include-data-files=$DevelopmentRuntimeProfilePath=runtime-profile.dev.json"
            $NuitkaArguments[-1]
        )
    }
    # Nuitka writes normal progress to stderr. Under redirected PowerShell
    # streams that becomes NativeCommandError; use its exit code as the build
    # verdict so successful compilation is not aborted before manifest/hash
    # generation. All nonzero compiler exit codes still fail below.
    $CompilerErrorActionPreferenceOriginal = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $Python @NuitkaArguments
    }
    finally {
        $ErrorActionPreference = $CompilerErrorActionPreferenceOriginal
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Nuitka build failed with exit code $LASTEXITCODE"
    }
    $NuitkaReportText = Get-Content -LiteralPath (
        Join-Path $TemporaryDirectory "nuitka-report.xml"
    ) -Raw -Encoding UTF8
    foreach ($ModuleName in @(
        "capture_process",
        "damage_hook"
    )) {
        $ModulePattern = '<module\s+name="' + [regex]::Escape($ModuleName) + '"'
        if ($NuitkaReportText -match $ModulePattern) {
            throw "Release unexpectedly contains unused legacy hook module: $ModuleName"
        }
    }

    $OutputPath = Join-Path $OutputDirectoryPath $OutputName
    if (-not (Test-Path -LiteralPath $OutputPath -PathType Leaf)) {
        throw "EXE was not found after build: $OutputPath"
    }
    $ManifestPath = Join-Path $ProjectDir "windows_dpi.manifest"
    & $Python (Join-Path $ProjectDir "embed_windows_manifest.py") `
        $OutputPath --manifest $ManifestPath
    if ($LASTEXITCODE -ne 0) {
        throw "DPI manifest embedding failed with exit code $LASTEXITCODE"
    }

    $SignatureStatus = "NotSigned"
    $SignatureSubject = ""
    if ($OfficialRelease) {
        $Signature = Set-AuthenticodeSignature `
            -LiteralPath $OutputPath `
            -Certificate $SigningCertificate `
            -HashAlgorithm SHA256 `
            -TimestampServer $TimestampServer
        $SignatureStatus = [string]$Signature.Status
        $SignatureSubject = [string]$Signature.SignerCertificate.Subject
        $ActualThumbprint = Normalize-Thumbprint $Signature.SignerCertificate.Thumbprint
        if ($Signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
            throw "Authenticode verification failed: $($Signature.StatusMessage)"
        }
        if ($ActualThumbprint -ne $SigningThumbprint) {
            throw "The signed EXE does not use the requested publisher certificate"
        }
    }

    $ForbiddenArtifacts = @(
        Get-ChildItem -LiteralPath $OutputDirectoryPath -Recurse -Force |
            Where-Object {
                $_.Name -match '^(test_|debug_)' -or
                $_.Name -match '^runtime-profile' -or
                $_.Extension -in @('.py', '.pyw', '.pyc', '.pyo', '.pdb', '.map', '.xml') -or
                $_.Name -in @('__pycache__', '.pytest_cache')
            }
    )
    if ($ForbiddenArtifacts.Count -gt 0) {
        $Names = ($ForbiddenArtifacts | Select-Object -ExpandProperty FullName) -join '; '
        throw "Release output contains source, test, symbol, or debug artifacts: $Names"
    }

    $OutputItem = Get-Item -LiteralPath $OutputPath
    $OutputHash = (Get-FileHash -LiteralPath $OutputPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $ReleaseManifestPath = Join-Path $OutputDirectoryPath "release-manifest-$NormalizedBuildId.json"
    $ReleaseManifest = [ordered]@{
        schema_version = 1
        product = $DisplayName
        version = $AppVersion
        client_build = $ClientBuild
        build_id = $NormalizedBuildId
        runtime_profile_id = $RuntimeProfileId
        official = [bool]$OfficialRelease
        protected = [bool]$ProtectedRelease
        capture_backend = "windows_raw"
        passive_capture = $true
        requires_npcap = $false
        native_ipv4_capture = "windows_raw_socket"
        native_ipv6_capture = "windivert_sniff_receive_only"
        capability_signing_key_id = $CapabilitySigningKeyId
        capability_public_keys = $CapabilityPublicKeys
        filename = $OutputName
        size = [int64]$OutputItem.Length
        sha256 = $OutputHash
        publisher_thumbprint = $SigningThumbprint
        signature_status = $SignatureStatus
        signature_subject = $SignatureSubject
        source_included = $false
        tests_included = $false
        debug_symbols_included = $false
        resource_hashes = $ResourceHashes
        generated_at_utc = [DateTime]::UtcNow.ToString('o')
    }
    Write-Utf8NoBom $ReleaseManifestPath ($ReleaseManifest | ConvertTo-Json -Depth 8)

    $AllowlistPath = Join-Path $OutputDirectoryPath "build-allowlist-$NormalizedBuildId.json"
    $Allowlist = [ordered]@{
        schema_version = 2
        builds = @(
            [ordered]@{
                build_id = $NormalizedBuildId
                client_build = $ClientBuild
                publisher_thumbprint = $SigningThumbprint
                runtime_profile_id = $RuntimeProfileId
                official = [bool]$OfficialRelease
                protected = [bool]$ProtectedRelease
                capability_signing_key_id = $CapabilitySigningKeyId
                enabled = [bool]($OfficialRelease -or $ProtectedRelease)
            }
        )
    }
    Write-Utf8NoBom $AllowlistPath ($Allowlist | ConvertTo-Json -Depth 5)

    $UpdateMetadataPath = Join-Path $OutputDirectoryPath "update.json"
    $UpdateMetadata = [ordered]@{
        # Keep the updater's release key numeric so already-published v0.1.7
        # clients can discover lettered hotfixes through CLIENT_BUILD revision.
        latest_version = $UpdateVersion
        display_version = $AppVersion
        client_build = $ClientBuild
        build_id = $NormalizedBuildId
        runtime_profile_id = $RuntimeProfileId
        filename = $OutputName
        size = [int64]$OutputItem.Length
        sha256 = $OutputHash
        publisher_thumbprint = $SigningThumbprint
        signature_required = [bool]$OfficialRelease
        protected = [bool]$ProtectedRelease
        notes = $ReleaseNotes
        required = $false
    }
    Write-Utf8NoBom $UpdateMetadataPath ($UpdateMetadata | ConvertTo-Json -Depth 5)
    if ($ReleaseNotes) {
        $ReleaseNotesOutputPath = Join-Path `
            $OutputDirectoryPath "$ReleaseNotesLabel-v$AppVersion.txt"
        Write-Utf8NoBom $ReleaseNotesOutputPath ($ReleaseNotes + "`n")
    }

    if ($OfficialRelease) {
        Add-Type -AssemblyName System.Security
        $ContentInfo = New-Object System.Security.Cryptography.Pkcs.ContentInfo `
            (,[System.IO.File]::ReadAllBytes($ReleaseManifestPath))
        $SignedCms = New-Object System.Security.Cryptography.Pkcs.SignedCms `
            ($ContentInfo, $true)
        $CmsSigner = New-Object System.Security.Cryptography.Pkcs.CmsSigner `
            $SigningCertificate
        $SignedCms.ComputeSignature($CmsSigner)
        [System.IO.File]::WriteAllBytes(
            "$ReleaseManifestPath.p7s",
            $SignedCms.Encode()
        )
    }

    [pscustomobject]@{
        Path = $OutputPath
        Version = $AppVersion
        ClientBuild = $ClientBuild
        BuildId = $NormalizedBuildId
        RuntimeProfileId = $RuntimeProfileId
        Official = [bool]$OfficialRelease
        Protected = [bool]$ProtectedRelease
        SignatureStatus = $SignatureStatus
        Sha256 = $OutputHash
        Manifest = $ReleaseManifestPath
    }
}
finally {
    Pop-Location
    if (Test-Path -LiteralPath $TemporaryDirectory) {
        Remove-Item -LiteralPath $TemporaryDirectory -Recurse -Force
    }
}
