<#
.SYNOPSIS
  Make the Windows release package of OpenScope GUI (stage-220).

.DESCRIPTION
  One command. Windows PowerShell 5.1. Steps:
   1. Both repositories (this GUI repo and the OpenScope-2C53T product repo next
      to it) must have no changes and no untracked files.
   2. The version in pyproject.toml and openscope_gui/__init__.py must be
      -Version. If not, the script writes -Version into both files and stops
      (exit 3): commit the two files, then run again.
   3. Exports HEAD of both repositories (git archive) and installs the two
      packages from the export into the build venv (no download, no deps).
   4. Checks the firmware image (manifest, check_image(), product HEAD, no
      "-dirty" name) and the build dependency commits.
   5. PyInstaller, one-folder, windowed, from packaging/openscope-gui.spec.
   6. Adds firmware\ (image, manifest, SOURCE.txt), LICENSE.txt,
      THIRD-PARTY-NOTICES.txt, README.txt and build_info.json.
   7. Writes <workspace>\releases\openscope-gui-<version>\ (the package folder)
      and <workspace>\releases\openscope-gui-<version>-upload\ with the zip and
      SHA256SUMS.txt.
  The releases folder is in no repository. Never add the zip or the image to a
  repository.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File packaging\build_release.ps1 -Version 0.2.0
#>
[CmdletBinding()]
param(
    [string]$Version = "0.2.0",
    [string]$Image = "",            # default: <workspace>\images\guest-coldtrace-<product short HEAD>.bin
    [string]$Workspace = "",        # default: the folder that holds this repository
    [string]$BuildVenv = "",        # default: <workspace>\venvs\openscope-build
    [switch]$Force                  # remove an earlier package of the same version first
)

$ErrorActionPreference = "Stop"
$t0 = Get-Date

function Stop-Build([string]$msg, [int]$code = 1) {
    Write-Host "ERROR: $msg" -ForegroundColor Red
    exit $code
}

function Invoke-Git([string]$repo, [string[]]$gitArgs) {
    $out = & git -C $repo @gitArgs
    if ($LASTEXITCODE -ne 0) { Stop-Build "git -C $repo $($gitArgs -join ' ') failed" }
    return $out
}

function Invoke-Step([string]$what, [string]$exe, [string[]]$exeArgs) {
    & $exe @exeArgs
    if ($LASTEXITCODE -ne 0) { Stop-Build "$what failed (exit $LASTEXITCODE)" }
}

# --- paths -------------------------------------------------------------------
$Gui = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Workspace) { $Workspace = (Resolve-Path (Join-Path $Gui "..")).Path }
$Product = Join-Path $Workspace "OpenScope-2C53T"
if (-not $BuildVenv) { $BuildVenv = Join-Path $Workspace "venvs\openscope-build" }
$Py = Join-Path $BuildVenv "Scripts\python.exe"
$Releases = Join-Path $Workspace "releases"
foreach ($p in @($Product, $Py)) { if (-not (Test-Path $p)) { Stop-Build "not found: $p" } }
if ($Version -notmatch '^\d+\.\d+\.\d+$') { Stop-Build "version '$Version' is not N.N.N" }

$Name = "openscope-gui-$Version"
$Package = Join-Path $Releases $Name
$Upload = Join-Path $Releases "$Name-upload"
$Stage = Join-Path $Releases "_build-$Version"
$Zip = Join-Path $Upload "$Name-win64.zip"
$Sums = Join-Path $Upload "SHA256SUMS.txt"

# --- 1. clean trees ------------------------------------------------------------
foreach ($repo in @($Gui, $Product)) {
    $dirty = Invoke-Git $repo @("status", "--porcelain")
    if ($dirty) { Stop-Build "$repo has changes or untracked files:`n$($dirty -join "`n")" }
}
Write-Host "trees     clean (GUI and product)"

# --- 2. version ----------------------------------------------------------------
$tools = Join-Path $Gui "packaging\release_tools.py"
$vers = (& $Py -I $tools versions $Gui)
if ($LASTEXITCODE -ne 0) { Stop-Build "cannot read the versions" }
$vers = "$vers".Trim().Split(" ")
if ($vers[0] -ne $Version -or $vers[1] -ne $Version) {
    Invoke-Step "version stamp" $Py @("-I", $tools, "stamp", $Gui, $Version)
    Stop-Build "version stamped to $Version (was $($vers -join ' / ')). Commit pyproject.toml and openscope_gui/__init__.py, then run again." 3
}
Write-Host "version   $Version"

# --- heads and tags ------------------------------------------------------------
$GuiCommit = "$(Invoke-Git $Gui @('rev-parse', 'HEAD'))".Trim()
$ProductCommit = "$(Invoke-Git $Product @('rev-parse', 'HEAD'))".Trim()
$GuiTag = "$(& git -C $Gui tag --points-at HEAD)".Trim()
$ProductTag = "$(& git -C $Product tag --points-at HEAD)".Trim()
Write-Host "GUI       $GuiCommit $GuiTag"
Write-Host "product   $ProductCommit $ProductTag"
if (-not $Image) {
    $Image = Join-Path $Workspace ("images\guest-coldtrace-" + $ProductCommit.Substring(0, 7) + ".bin")
}
if (-not (Test-Path $Image)) { Stop-Build "firmware image not found: $Image (build it with build.sh guest-coldtrace, then openscope manifest)" }
$Image = (Resolve-Path $Image).Path

# --- output folders ------------------------------------------------------------
New-Item -ItemType Directory -Force $Releases | Out-Null
foreach ($d in @($Package, $Upload, $Stage)) {
    if (Test-Path $d) {
        if ($Force -or $d -eq $Stage) { Remove-Item -Recurse -Force $d }
        else { Stop-Build "$d exists. Use -Force to make this version again." }
    }
}
New-Item -ItemType Directory $Stage | Out-Null

# --- 3. export and install -----------------------------------------------------
$Exp = Join-Path $Stage "src"
New-Item -ItemType Directory $Exp | Out-Null
$guiTar = Join-Path $Stage "gui.tar"
$libTar = Join-Path $Stage "lib.tar"
Invoke-Git $Gui @("archive", "--format=tar", "--prefix=gui/", "-o", $guiTar, "HEAD") | Out-Null
Invoke-Git $Product @("archive", "--format=tar", "--prefix=product/", "-o", $libTar, "HEAD", "tools/openscope_host") | Out-Null
Invoke-Step "tar (GUI)" "tar.exe" @("-xf", $guiTar, "-C", $Exp)
Invoke-Step "tar (library)" "tar.exe" @("-xf", $libTar, "-C", $Exp)
$ExpGui = Join-Path $Exp "gui"
$ExpLib = Join-Path $Exp "product\tools\openscope_host"
Invoke-Step "pip install" $Py @("-m", "pip", "install", "--quiet", "--no-deps", "--no-index",
    "--no-build-isolation", "--force-reinstall", $ExpLib, $ExpGui)
$installed = (& $Py -I -c "import openscope, openscope_gui; print(openscope.__version__, openscope_gui.__version__, openscope_gui.__file__)")
if ($LASTEXITCODE -ne 0) { Stop-Build "the installed packages do not import" }
Write-Host "installed openscope / openscope_gui: $installed"
if ("$installed".Split(" ")[1] -ne $Version) { Stop-Build "installed openscope_gui is not $Version" }
if ("$installed" -match [regex]::Escape($Gui)) { Stop-Build "openscope_gui imports from the repository (editable install), not from the build venv" }

# --- 4. firmware and dependencies --------------------------------------------
$expTools = Join-Path $ExpGui "packaging\release_tools.py"
Invoke-Step "firmware check" $Py @("-I", $expTools, "precheck", "--image", $Image, "--product-head", $ProductCommit)
Invoke-Step "dependency check" $Py @("-I", $expTools, "deps", $Product)

# --- 5. PyInstaller ------------------------------------------------------------
$tb = Get-Date
Invoke-Step "PyInstaller" $Py @("-I", "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
    "--distpath", (Join-Path $Stage "dist"), "--workpath", (Join-Path $Stage "work"),
    (Join-Path $ExpGui "packaging\openscope-gui.spec"))
$pyiSecs = [math]::Round(((Get-Date) - $tb).TotalSeconds, 1)
Move-Item (Join-Path $Stage "dist\openscope-gui") $Package

# --- 6. package contents -------------------------------------------------------
Invoke-Step "assemble" $Py @("-I", $expTools, "assemble", "--gui", $ExpGui, "--product", $Product,
    "--image", $Image, "--out", $Package, "--version", $Version, "--gui-commit", $GuiCommit,
    "--product-commit", $ProductCommit, "--gui-tag", $GuiTag, "--product-tag", $ProductTag)

# --- 7. zip and sums -----------------------------------------------------------
New-Item -ItemType Directory $Upload | Out-Null
Invoke-Step "zip" $Py @("-I", $expTools, "zip", $Package, $Zip, $Sums)
Remove-Item -Recurse -Force $Stage

$files = Get-ChildItem -Recurse -File $Package
$size = ($files | Measure-Object -Property Length -Sum).Sum
$zipSize = (Get-Item $Zip).Length
$secs = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
Write-Host ""
Write-Host "package   $Package"
Write-Host ("          {0} files, {1:N0} B ({2:N1} MB)" -f $files.Count, $size, ($size / 1MB))
Write-Host ("zip       {0}  {1:N0} B ({2:N1} MB)" -f $Zip, $zipSize, ($zipSize / 1MB))
Write-Host "sums      $Sums"
Get-Content $Sums | ForEach-Object { Write-Host "          $_" }
Write-Host "time      $secs s (PyInstaller $pyiSecs s)"
if (-not $GuiTag -or -not $ProductTag) {
    Write-Host "NOTE: a commit has no tag. SOURCE.txt says '(no tag)' for it."
}
Write-Host "Before you publish: push both commits (and tags), or the URLs in SOURCE.txt do not resolve."
exit 0
