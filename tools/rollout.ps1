<#
.SYNOPSIS
  Onboard one or many ZIG repositories to the wm_zig_config publisher.

.DESCRIPTION
  Seeds tools/, .githooks/, a starter wm_zig.json and the required
  .gitattributes lines, then runs `git config core.hooksPath .githooks`.

  DRY-RUN BY DEFAULT. Nothing is written without -Apply. It NEVER commits and
  never touches your index or working state beyond the files it seeds.

  .git/hooks is not cloneable, so enabling core.hooksPath is a one-time local
  command per clone. That is the only manual step and there is no way around it.

.EXAMPLE
  # Read the report first - changes nothing:
  powershell -File tools\install_wm_zig_hook.ps1 -All C:\ShivamDev\Github\ZIGS

.EXAMPLE
  powershell -File tools\install_wm_zig_hook.ps1 -RepoPath C:\...\MBK_TEST_ZIG_FAST -Apply
#>
[CmdletBinding()]
param(
    [string] $RepoPath,
    [string] $All,
    [string] $Prefix,
    [string] $CustomerName,
    [string] $KitPath,
    [string] $ConfigUrl = 'https://github.com/sp-wal-rd/wm_zig_config.git',
    [switch] $Apply,
    [switch] $Force
)

$ErrorActionPreference = 'Stop'

if (-not $KitPath) { $KitPath = Split-Path -Parent $PSScriptRoot }
$KitTools = Join-Path $KitPath 'tools'
if (-not (Test-Path (Join-Path $KitPath 'wm_zig/wm_zig.py'))) {
    throw "Kit incomplete: wm_zig/wm_zig.py not found under $KitPath. Point -KitPath at a wm_zig_config clone."
}

function Find-Python {
    # The smoke-test is load-bearing: Windows ships an App Execution Alias for
    # python3.exe/python.exe that satisfies Get-Command, prints "Python was not
    # found..." to stderr and opens the Microsoft Store. It must be rejected.
    $keep = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        foreach ($c in 'python3', 'python', 'py') {
            if (-not (Get-Command $c -ErrorAction SilentlyContinue)) { continue }
            $probe = ''
            try { $probe = (& $c -c "print('wmzig-ok')" 2>&1 | Out-String).Trim() } catch { continue }
            if ($LASTEXITCODE -eq 0 -and $probe -match 'wmzig-ok') { return $c }
        }
    } finally {
        $ErrorActionPreference = $keep
    }
    return $null
}
$Py = Find-Python
if (-not $Py) { throw 'No working Python on PATH.' }

function Git-In([string]$Repo, [string[]]$GitArgs) {
    $out = & git -C $Repo @GitArgs 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return ($out | Out-String).Trim()
}

function Get-Prefix([string]$Repo, [string]$Override) {
    if ($Override) { return $Override }
    $name = Split-Path -Leaf $Repo
    foreach ($suffix in '_TEST_ZIG_FAST', '_TEST_ZIG') {
        if ($name.EndsWith($suffix)) { return $name.Substring(0, $name.Length - $suffix.Length) }
    }
    return $null
}

# Prefixes already claimed by a published spec, so a second clone of the same
# program cannot silently take it over.
function Normalize-Remote([string]$Url) {
    if (-not $Url) { return '' }
    $u = $Url.Trim()
    $u = $u -replace '^[a-zA-Z0-9+.-]+://', ''
    $u = $u -replace '^[^/@]*@', ''
    $u = $u -replace ':', '/'
    if ($u.EndsWith('.git')) { $u = $u.Substring(0, $u.Length - 4) }
    return $u.TrimEnd('/').ToLowerInvariant()
}

# prefix -> @{ Label; Remote }. Keeping the owning remote is what lets a repo
# recognise its OWN published spec instead of treating it as a rival claim.
$claimed = @{}
$idx = Join-Path $KitPath 'index.json'
if (Test-Path $idx) {
    try {
        $idxDoc = Get-Content $idx -Raw | ConvertFrom-Json
        foreach ($z in $idxDoc.zigs) {
            if (-not $z.prefix) { continue }
            $owner = ''
            foreach ($rel in @($z.file, $z.released_file)) {
                if (-not $rel) { continue }
                $specPath = Join-Path $KitPath ($rel -replace '/', [IO.Path]::DirectorySeparatorChar)
                if (Test-Path $specPath) {
                    try { $owner = (Get-Content $specPath -Raw | ConvertFrom-Json).repo } catch { }
                    if ($owner) { break }
                }
            }
            $claimed[$z.prefix] = @{ Label = 'already published'; Remote = (Normalize-Remote $owner) }
        }
    } catch { }
}

$targets = @()
if ($All) {
    if (-not (Test-Path $All)) { throw "-All path not found: $All" }
    Get-ChildItem -Path $All -Directory -Recurse -Depth 4 -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq '.git' } |
        ForEach-Object { $targets += (Split-Path -Parent $_.FullName) }
    $targets = $targets | Sort-Object -Unique
} elseif ($RepoPath) {
    $targets = @($RepoPath)
} else {
    throw 'Specify -RepoPath <repo> or -All <root>.'
}

if (-not $Apply) {
    Write-Host ''
    Write-Host '  DRY RUN - nothing will be written. Re-run with -Apply to act.' -ForegroundColor Yellow
}
Write-Host ''
Write-Host ('{0,-46} {1,-12} {2,-9} {3}' -f 'REPO', 'PREFIX', 'BRANCH', 'VERDICT')
Write-Host ('-' * 110)

$results = @()
$reportRoot = if ($All) { (Resolve-Path $All).Path } else { $null }
foreach ($repo in $targets) {
    $name = Split-Path -Leaf $repo
    # Several prefixes have two or more checkouts with identical folder names;
    # a bare basename makes the conflict list impossible to act on.
    $rel = if ($reportRoot -and $repo.StartsWith($reportRoot)) {
        $repo.Substring($reportRoot.Length).TrimStart('\', '/')
    } else { $repo }
    $verdict = ''
    $prefix = $null
    $branch = ''

    $top = Git-In $repo @('rev-parse', '--show-toplevel')
    if (-not $top) { $verdict = 'SKIP (not a git repo)' }

    if (-not $verdict) {
        $branch = Git-In $repo @('rev-parse', '--abbrev-ref', 'HEAD')
        if (-not (Test-Path (Join-Path $repo 'config.py'))) {
            $verdict = 'SKIP (no top-level config.py)'
        }
    }
    if (-not $verdict) {
        $prefix = Get-Prefix $repo $Prefix
        if (-not $prefix) { $verdict = 'NEEDS -Prefix (name has no _TEST_ZIG suffix)' }
    }

    $publish = $true

    # Archived / reference checkouts must never publish. They frequently share
    # an origin with the live checkout, so expect_remote alone cannot separate
    # them - only their location can.
    if (-not $verdict -and ($rel.Split([char[]]('\','/'))) -match '^(Backup|Ref|Archive|Old)$') {
        $verdict = 'ARCHIVE (under Backup/Ref) - seeding with publish=false'
        $publish = $false
    }

    if (-not $verdict -and $claimed.ContainsKey($prefix)) {
        $mine  = Normalize-Remote (Git-In $repo @('remote', 'get-url', 'origin'))
        $owner = $claimed[$prefix].Remote
        if ($owner -and $mine -and $owner -eq $mine) {
            # Same origin: this IS the publisher for that prefix, not a rival.
            $verdict = "OWNER ($prefix - matches the published remote)"
        } else {
            $verdict = "CONFLICT ($prefix $($claimed[$prefix].Label) by $owner) - seeding with publish=false"
            $publish = $false
        }
    }
    if (-not $verdict -or $verdict -like 'OWNER*') {
        if (-not $verdict) {
            $claimed[$prefix] = @{ Label = "claimed by $rel"; Remote = (Normalize-Remote (Git-In $repo @('remote', 'get-url', 'origin'))) }
        }
        $verdict = if (Test-Path (Join-Path $repo 'wm_zig.json')) {
            if ($Force) { 'RE-SEED (wm_zig.json overwritten)' } else { 'UPDATE (keeping existing wm_zig.json)' }
        } else { 'ONBOARD' }
    }

    Write-Host ('{0,-46} {1,-12} {2,-9} {3}' -f
        $(if ($rel.Length -gt 46) { '...' + $rel.Substring($rel.Length - 43) } else { $rel }),
        $(if ($prefix) { $prefix } else { '-' }),
        $(if ($branch) { $branch } else { '-' }),
        $verdict)

    $results += [pscustomobject]@{ Repo = $repo; Rel = $rel; Prefix = $prefix; Verdict = $verdict; Publish = $publish }

    if (-not $Apply -or $verdict -like 'SKIP*' -or $verdict -like 'NEEDS*') { continue }

    # ---- write ----
    # One file, then let it install itself. Everything the repo needs - the
    # hook, the .gitattributes lines, core.hooksPath - comes from wm_zig.py.
    $wmDir = Join-Path $repo 'wm_zig'
    New-Item -ItemType Directory -Force -Path $wmDir | Out-Null
    [IO.File]::WriteAllBytes(
        (Join-Path $wmDir 'wm_zig.py'),
        [IO.File]::ReadAllBytes((Join-Path $KitPath 'wm_zig/wm_zig.py')))

    Push-Location $repo
    & $Py -X utf8 (Join-Path $wmDir 'wm_zig.py') --install 2>&1 | ForEach-Object {
        Write-Host ('    ' + $_) -ForegroundColor DarkGray }
    Pop-Location

    # A declaration is only written when a default needs overriding.
    $declPath = Join-Path $repo 'wm_zig.json'
    if ($Force -or (-not (Test-Path $declPath) -and (-not $publish -or $CustomerName))) {
        $decl = "{`n"
        if ($CustomerName) { $decl += "  `"customer_name`": `"$CustomerName`",`n" }
        if (-not $publish) { $decl += "  `"publish`": false,`n" }
        $decl = $decl.TrimEnd(",`n".ToCharArray()) + "`n}`n"
        [IO.File]::WriteAllText($declPath, $decl, [Text.UTF8Encoding]::new($false))
        Write-Host '    wrote wm_zig.json (override only)' -ForegroundColor DarkGray
    }

    Push-Location $repo
    $selftest = & $Py -X utf8 (Join-Path $wmDir 'wm_zig.py') --dry-run 2>&1 | Out-String
    Pop-Location
    Write-Host ('    self-test: ' + ($selftest.Trim() -split "`n" | Select-Object -Last 1)) -ForegroundColor DarkGray
}

Write-Host ''
if ($Apply) {
    $done = $results | Where-Object { $_.Verdict -notlike 'SKIP*' -and $_.Verdict -notlike 'NEEDS*' }
    Write-Host "Seeded $($done.Count) repository(ies). Nothing was committed." -ForegroundColor Green
    Write-Host ''
    Write-Host 'For each repo, commit what was seeded:' -ForegroundColor Cyan
    Write-Host '    git add wm_zig .githooks .gitattributes'
    Write-Host '    git commit -m "chore: wm_zig publisher"'
    Write-Host ''
    Write-Host 'The next push publishes automatically.'
} else {
    $need = $results | Where-Object { $_.Verdict -like 'NEEDS*' -or $_.Verdict -like 'CONFLICT*' }
    if ($need) {
        Write-Host 'Resolve these by hand before -Apply:' -ForegroundColor Yellow
        $need | ForEach-Object { Write-Host ('  ' + $_.Rel + ' : ' + $_.Verdict) }
        Write-Host ''
    }
    Write-Host 'Re-run with -Apply to write.' -ForegroundColor Yellow
}
