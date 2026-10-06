# 节点测评 · 启动脚本（Windows PowerShell 5.1+）
# 首次运行会：找到/安装 Python → 建立 app\.venv 并安装依赖 → 找到 Clash Verge 内核或下载 Mihomo → 打开本地页面。
param([int]$Port = 8775)
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$App = Join-Path $Root 'app'
$Url = "http://127.0.0.1:$Port"
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

function Test-Ready { try { $null = Invoke-RestMethod "$Url/api/profiles" -TimeoutSec 2; return $true } catch { return $false } }

function Find-Python {
    $cands = @(
        @{ exe = 'py'; pre = @('-3') },
        @{ exe = 'python'; pre = @() },
        @{ exe = 'python3'; pre = @() },
        @{ exe = "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe"; pre = @() },
        @{ exe = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"; pre = @() },
        @{ exe = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"; pre = @() }
    )
    foreach ($c in $cands) {
        if (-not (Get-Command $c.exe -ErrorAction SilentlyContinue)) { continue }
        $pre = $c.pre
        try { $out = @(& $c.exe @pre -c "import sys;print('%d.%d' % sys.version_info[:2]);print(sys.executable)" 2>$null) } catch { continue }
        if ($LASTEXITCODE -ne 0 -or $out.Count -lt 2) { continue }   # 跳过微软商店的 python 占位程序
        if ([version]$out[0] -ge [version]'3.9') { return $out[1] }
    }
    return $null
}

function Fail($msg) {
    Write-Host ''
    Write-Host $msg -ForegroundColor Red
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show($msg, '节点测评') | Out-Null
    } catch {}
    exit 1
}

try {
    if (Test-Ready) { Start-Process $Url; exit 0 }

    if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) {
        Fail '找不到 curl.exe。需要 Windows 10 1803 或更新版本（系统自带 curl）。'
    }

    $python = Find-Python
    if (-not $python) {
        Write-Host '未找到 Python 3.9 以上版本。' -ForegroundColor Yellow
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Write-Host '正在用 winget 安装 Python 3.12（仅当前用户）…'
            winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
            $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
            $python = Find-Python
        }
        if (-not $python) {
            Fail "请先安装 Python 3.9+：https://www.python.org/downloads/ （安装时勾选 Add python.exe to PATH），然后重新双击「启动测评.cmd」。"
        }
    }

    $venv = Join-Path $App '.venv'
    $vpy = Join-Path $venv 'Scripts\python.exe'
    if (-not (Test-Path $vpy)) {
        Write-Host "使用 $python 创建运行环境 app\.venv …"
        & $python -m venv $venv
        if ($LASTEXITCODE -ne 0) { Fail '创建 Python 虚拟环境失败。' }
    }

    $req = Join-Path $Root 'requirements.txt'
    $stamp = Join-Path $venv 'requirements.sha256'
    $hash = (Get-FileHash $req -Algorithm SHA256).Hash
    if (-not (Test-Path $stamp) -or (Get-Content $stamp -Raw).Trim() -ne $hash) {
        Write-Host '安装依赖（PyYAML / Pillow / openpyxl），首次约需 1 分钟…'
        & $vpy -m pip install --disable-pip-version-check --timeout 20 -q -r $req
        if ($LASTEXITCODE -ne 0) {
            Write-Host '官方源失败，改用清华镜像重试…' -ForegroundColor Yellow
            & $vpy -m pip install --disable-pip-version-check --timeout 20 -q -r $req -i https://pypi.tuna.tsinghua.edu.cn/simple
        }
        if ($LASTEXITCODE -ne 0) { Fail '依赖安装失败，请检查网络后重试。' }
        Set-Content -Path $stamp -Value $hash -Encoding ascii
    }

    & $vpy (Join-Path $App 'node_lab.py') --ensure-core
    if ($LASTEXITCODE -ne 0) { Fail '没有可用的 Mihomo 内核：请安装 Clash Verge Rev，或检查能否访问 GitHub 后重试。' }

    $runtime = Join-Path $App 'runtime'
    New-Item -ItemType Directory -Force -Path $runtime | Out-Null
    Start-Process -FilePath $vpy -ArgumentList @(('"' + (Join-Path $App 'node_lab.py') + '"'), $Port) -WorkingDirectory $App -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $runtime 'server-out.log') -RedirectStandardError (Join-Path $runtime 'server-error.log') | Out-Null
    for ($i = 0; $i -lt 60; $i++) { if (Test-Ready) { break }; Start-Sleep -Milliseconds 250 }
    if (-not (Test-Ready)) { Fail '本地服务启动失败，请查看 app\runtime\server-error.log。' }
    Start-Process $Url
} catch {
    Fail ("启动失败：" + $_.Exception.Message)
}
