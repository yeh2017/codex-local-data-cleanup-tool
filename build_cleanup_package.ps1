param(
    [string]$PythonExe = ""
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$workspaceRoot = Split-Path -Parent $projectRoot
$outputRoot = Join-Path $workspaceRoot 'outputs'
$packageRoot = Join-Path $outputRoot 'chatgpt_codex_local_history_cleanup_tool_windows_x64'
$zipPath = Join-Path $outputRoot 'chatgpt_codex_local_history_cleanup_tool_windows_x64.zip'
$taskTempRoot = [IO.Path]::GetTempPath()
$buildRoot = Join-Path $taskTempRoot ('codex-cleanup-build-' + [guid]::NewGuid().ToString('N'))
$distRoot = Join-Path $buildRoot 'dist'
$workRoot = Join-Path $buildRoot 'work'
$specRoot = Join-Path $buildRoot 'spec'
$appName = 'ChatGPT-Codex Local History Cleanup Tool'
$iconPath = Join-Path $projectRoot 'assets\codex_cleanup_tool.ico'

if (-not $PythonExe) {
    $localBuilder = Join-Path $projectRoot '.build-venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $localBuilder) {
        $PythonExe = $localBuilder
    } else {
        $PythonExe = (Get-Command python -ErrorAction Stop).Source
    }
}

New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null
$resolvedOutput = [IO.Path]::GetFullPath($outputRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
$resolvedTemp = [IO.Path]::GetFullPath($taskTempRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
foreach ($target in @($packageRoot, $zipPath)) {
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

try {
    & $PythonExe -m PyInstaller `
        --noconfirm `
        --clean `
        --onedir `
        --windowed `
        --name $appName `
        --icon $iconPath `
        --distpath $distRoot `
        --workpath $workRoot `
        --specpath $specRoot `
        (Join-Path $projectRoot 'frozen_entry.py')
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller 构建失败，错误代码：$LASTEXITCODE"
    }

    Move-Item -LiteralPath (Join-Path $distRoot $appName) -Destination $packageRoot
    Copy-Item -LiteralPath (Join-Path $projectRoot 'diagnose_codex_cleanup_tool.bat') -Destination $packageRoot

    Compress-Archive -Path $packageRoot -DestinationPath $zipPath -CompressionLevel Optimal
} finally {
    if (Test-Path -LiteralPath $buildRoot) {
        Remove-Item -LiteralPath $buildRoot -Recurse -Force
    }
}

Write-Host "Package: $packageRoot"
Write-Host "ZIP:     $zipPath"
