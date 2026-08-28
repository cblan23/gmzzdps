param(
    [string]$OutputDirectory = "dist",
    [string]$OutputFilename = ""
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectDir ".venv-build310\Scripts\python.exe"
$CapstoneDll = Join-Path $ProjectDir ".venv-build310\Lib\site-packages\capstone\lib\capstone.dll"
$ProductName = "$([char]0x53E8)$([char]0x53E8)$([char]0x8BE1)$([char]0x79D8)$([char]0x52A9)$([char]0x624B)"
$Description = "$ProductName$([char]0x56E2)$([char]0x961F)$([char]0x4F24)$([char]0x5BB3)$([char]0x7EDF)$([char]0x8BA1)"
$OutputName = if ($OutputFilename) {
    $OutputFilename
} else {
    "$ProductName-DPS-METER-v0.0.5.exe"
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
        --include-data-dir=assets=assets `
        --include-data-files=skill_names.json=skill_names.json `
        --include-data-files=skill_metadata.json=skill_metadata.json `
        --include-data-files=monster_metadata.json=monster_metadata.json `
        --include-data-files=boss_allowlist.txt=boss_allowlist.txt `
        --include-data-files=cacert.pem=cacert.pem `
        --file-version=0.0.5.0 `
        --product-version=0.0.5.0 `
        --product-name="$ProductName DPS METER" `
        --file-description=$Description `
        --copyright=$ProductName `
        dps_meter.pyw

    if ($LASTEXITCODE -ne 0) {
        throw "Nuitka build failed with exit code $LASTEXITCODE"
    }

    $OutputPath = Join-Path $OutputDirectoryPath $OutputName
    if (-not (Test-Path -LiteralPath $OutputPath)) {
        throw "EXE was not found after build: $OutputPath"
    }
    Get-Item -LiteralPath $OutputPath
}
finally {
    Pop-Location
}
