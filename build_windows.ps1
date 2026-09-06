param(
    [string]$Python = "python",
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppName = "JDBox_Athena_v0.6.0"
Set-Location -LiteralPath $ProjectRoot

if ($env:OS -ne "Windows_NT") {
    throw "The Windows executable must be built on Windows."
}

if (-not $SkipInstall) {
    & $Python -m pip install -e ".[windows-gui]"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install the Windows GUI build dependencies."
    }
}

$RequiredAssets = @(
    "uboot-ipq60xx-jdcloud_re-cs-02-260816_142236_3011049.bin",
    "ones20250-main-pure-ipq60xx-jdcloud_re-cs-02-squashfs-factory-26.08.30-10.38.06.bin",
    "npcap-1.88.exe",
    "README.md",
    "THIRD_PARTY_NOTICES.md"
)

foreach ($Asset in $RequiredAssets) {
    if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot $Asset) -PathType Leaf)) {
        throw "Missing required asset: $Asset"
    }
}

$ManifestPath = Join-Path $ProjectRoot "athena_gui.manifest"
$PyInstallerArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--windowed",
    "--onedir",
    "--name", $AppName,
    "--manifest", $ManifestPath,
    "--paths", "src",
    "--collect-all", "scapy",
    "--distpath", "dist",
    "--workpath", "build\pyinstaller",
    "--specpath", "build"
)

foreach ($Asset in $RequiredAssets) {
    $AssetPath = Join-Path $ProjectRoot $Asset
    $PyInstallerArgs += @("--add-data", "$AssetPath;.")
}

$PyInstallerArgs += "athena_gui.py"
& $Python @PyInstallerArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

$Executable = Join-Path $ProjectRoot "dist\$AppName\$AppName.exe"
Write-Host "Build complete: $Executable" -ForegroundColor Green
