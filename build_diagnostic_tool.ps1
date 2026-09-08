param(
    [string]$OutputDirectory = "diagnostic-dist",
    [string]$OutputFilename = ""
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectDir ".venv-build310\Scripts\python.exe"
$CapstoneDll = Join-Path $ProjectDir ".venv-build310\Lib\site-packages\capstone\lib\capstone.dll"
$SourcePath = Join-Path $ProjectDir "diagnostic_report.py"
$RuntimeProfilePath = Join-Path $ProjectDir "runtime-profile.dev.json"
$ProductName = "$([char]0x53E8)$([char]0x53E8)$([char]0x8BE1)$([char]0x79D8)$([char]0x95EE)$([char]0x9898)$([char]0x68C0)$([char]0x6D4B)$([char]0x5DE5)$([char]0x5177)"
$SourceText = Get-Content -LiteralPath $SourcePath -Raw -Encoding UTF8
$VersionMatch = [regex]::Match(
    $SourceText,
    '(?m)^\s*TOOL_VERSION\s*=\s*"(?<version>\d+(?:\.\d+){2,3})(?:\+[^"\r\n]*)?"\s*$'
)
if (-not $VersionMatch.Success) {
    throw "TOOL_VERSION was not found in $SourcePath"
}
$ToolVersion = $VersionMatch.Groups["version"].Value
$FileVersion = if (($ToolVersion.Split('.')).Count -eq 4) {
    $ToolVersion
} else {
    "$ToolVersion.0"
}
$OutputName = if ($OutputFilename) {
    $OutputFilename
} else {
    "$ProductName-v$ToolVersion.exe"
}
$OutputDirectoryPath = if ([System.IO.Path]::IsPathRooted($OutputDirectory)) {
    [System.IO.Path]::GetFullPath($OutputDirectory)
} else {
    Join-Path $ProjectDir $OutputDirectory
}

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python was not found: $Python"
}
if (-not (Test-Path -LiteralPath $CapstoneDll)) {
    throw "Capstone DLL was not found: $CapstoneDll"
}
if (-not (Test-Path -LiteralPath $RuntimeProfilePath -PathType Leaf)) {
    throw "Runtime profile was not found: $RuntimeProfilePath"
}
New-Item -ItemType Directory -Path $OutputDirectoryPath -Force | Out-Null

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:VSLANG = "1033"
$env:CL = "/utf-8"

Push-Location $ProjectDir
try {
    & $Python -m nuitka `
        --mode=onefile `
        --windows-console-mode=disable `
        --enable-plugin=tk-inter `
        --assume-yes-for-downloads `
        --jobs=4 `
        --remove-output `
        --output-dir=$OutputDirectoryPath `
        --output-filename=$OutputName `
        --windows-icon-from-ico=assets/app_icon.ico `
        --include-package=capstone `
        --include-data-files=$CapstoneDll=capstone/lib/capstone.dll `
        --include-data-files=assets/app_icon.ico=assets/app_icon.ico `
        --include-data-files=monster_metadata.json=monster_metadata.json `
        --include-data-files=boss_allowlist.txt=boss_allowlist.txt `
        --include-data-files=runtime-profile.dev.json=runtime-profile.json `
        --include-data-files=cacert.pem=cacert.pem `
        --file-version=$FileVersion `
        --product-version=$FileVersion `
        --product-name="$ProductName" `
        --file-description="$ProductName" `
        --copyright="$([char]0x53E8)$([char]0x53E8)$([char]0x8BE1)$([char]0x79D8)" `
        diagnostic_tool.pyw

    if ($LASTEXITCODE -ne 0) {
        throw "Nuitka build failed with exit code $LASTEXITCODE"
    }

$OutputPath = Join-Path $OutputDirectoryPath $OutputName
    if (-not (Test-Path -LiteralPath $OutputPath)) {
        throw "EXE was not found after build: $OutputPath"
    }
    $ManifestPath = Join-Path $ProjectDir "windows_dpi.manifest"
    & $Python (Join-Path $ProjectDir "embed_windows_manifest.py") `
        $OutputPath --manifest $ManifestPath
    if ($LASTEXITCODE -ne 0) {
        throw "DPI manifest embedding failed with exit code $LASTEXITCODE"
    }
    Get-Item -LiteralPath $OutputPath
}
finally {
    Pop-Location
}
