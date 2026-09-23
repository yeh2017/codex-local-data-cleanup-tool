param(
    [string]$PythonExe = ""
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$workspaceRoot = Split-Path -Parent $projectRoot
$outputRoot = Join-Path $workspaceRoot 'outputs'
$taskTempRoot = [IO.Path]::GetTempPath()
$buildRoot = Join-Path $taskTempRoot ('codex-cleanup-build-' + [guid]::NewGuid().ToString('N'))
$distRoot = Join-Path $buildRoot 'dist'
$workRoot = Join-Path $buildRoot 'work'
$specRoot = Join-Path $buildRoot 'spec'
$iconPath = Join-Path $projectRoot 'assets\codex_cleanup_tool.ico'

if (-not $PythonExe) {
    $localBuilder = Join-Path $projectRoot '.build-venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $localBuilder) {
        $PythonExe = $localBuilder
    } else {
        $PythonExe = (Get-Command python -ErrorAction Stop).Source
    }
}

$version = (& $PythonExe -c "from codex_cleanup_tool.version import APP_VERSION; print(APP_VERSION)").Trim()
$appName = (& $PythonExe -c "from codex_cleanup_tool.version import APP_EXECUTABLE_NAME; print(APP_EXECUTABLE_NAME)").Trim()
$packageBase = "codex_local_data_cleanup_tool_v${version}_windows_x64"
$packageRoot = Join-Path $outputRoot $packageBase
$zipPath = Join-Path $outputRoot ($packageBase + '.zip')
$checksumPath = $zipPath + '.sha256'

New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null
$resolvedOutput = [IO.Path]::GetFullPath($outputRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
$resolvedTemp = [IO.Path]::GetFullPath($taskTempRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
foreach ($target in @($packageRoot, $zipPath, $checksumPath)) {
    $resolvedTarget = [IO.Path]::GetFullPath($target)
    if (-not $resolvedTarget.StartsWith($resolvedOutput + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe output target: $resolvedTarget"
    }
}
$resolvedBuild = [IO.Path]::GetFullPath($buildRoot)
if (-not $resolvedBuild.StartsWith($resolvedTemp + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe temporary build target: $resolvedBuild"
}

& $PythonExe -c "import struct; assert struct.calcsize('P') * 8 == 64, '需要 64 位 Python 构建环境'; import PyInstaller"
if ($LASTEXITCODE -ne 0) {
    throw '未找到可用的 64 位 PyInstaller 构建环境。'
}

if (-not (Test-Path -LiteralPath $iconPath -PathType Leaf)) {
    throw "缺少程序图标：$iconPath"
}

if (Test-Path -LiteralPath $packageRoot) {
    Remove-Item -LiteralPath $packageRoot -Recurse -Force
}
if (Test-Path -LiteralPath $buildRoot) {
    Remove-Item -LiteralPath $buildRoot -Recurse -Force
}
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}
if (Test-Path -LiteralPath $checksumPath) {
    Remove-Item -LiteralPath $checksumPath -Force
}

try {
    New-Item -ItemType Directory -Force -Path $specRoot | Out-Null
    $versionParts = @($version.Split('.') | ForEach-Object { [int]$_ })
    while ($versionParts.Count -lt 4) { $versionParts += 0 }
    $versionTuple = ($versionParts[0..3] -join ', ')
    $versionFile = Join-Path $specRoot 'windows-version.txt'
    @"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($versionTuple), prodvers=($versionTuple), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'Community Project'),
    StringStruct('FileDescription', 'Codex Local Data Cleanup Tool'),
    StringStruct('FileVersion', '$version'),
    StringStruct('InternalName', '$appName'),
    StringStruct('OriginalFilename', '$appName.exe'),
    StringStruct('ProductName', 'Codex Local Data Cleanup Tool'),
    StringStruct('ProductVersion', '$version')
  ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
"@ | Set-Content -LiteralPath $versionFile -Encoding UTF8

    & $PythonExe -m PyInstaller `
        --noconfirm `
        --clean `
        --onedir `
        --windowed `
        --name $appName `
        --icon $iconPath `
        --version-file $versionFile `
        --distpath $distRoot `
        --workpath $workRoot `
        --specpath $specRoot `
        (Join-Path $projectRoot 'frozen_entry.py')
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller 构建失败，错误代码：$LASTEXITCODE"
    }

    Move-Item -LiteralPath (Join-Path $distRoot $appName) -Destination $packageRoot
    Copy-Item -LiteralPath (Join-Path $projectRoot 'diagnose_codex_cleanup_tool.bat') -Destination $packageRoot
    Copy-Item -LiteralPath (Join-Path $projectRoot 'README.md') -Destination $packageRoot
    Copy-Item -LiteralPath (Join-Path $projectRoot 'README.zh-CN.md') -Destination $packageRoot
    Copy-Item -LiteralPath (Join-Path $projectRoot 'LICENSE') -Destination $packageRoot

    Compress-Archive -Path $packageRoot -DestinationPath $zipPath -CompressionLevel Optimal
    $hash = Get-FileHash -LiteralPath $zipPath -Algorithm SHA256
    ("{0}  {1}" -f $hash.Hash, (Split-Path -Leaf $zipPath)) |
        Set-Content -LiteralPath $checksumPath -Encoding Ascii
} finally {
    if (Test-Path -LiteralPath $buildRoot) {
        Remove-Item -LiteralPath $buildRoot -Recurse -Force
    }
}

Write-Host "Package: $packageRoot"
Write-Host "ZIP:     $zipPath"
Write-Host "SHA256:  $checksumPath"
