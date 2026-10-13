[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$sourceConfigPath = Join-Path $projectRoot 'backend\config.ini'
$dockerConfigPath = Join-Path $projectRoot 'backend\config.docker.ini'
$dockerConfigChanged = $false

function Get-IniValue {
    param(
        [Parameter(Mandatory)][string]$Content,
        [Parameter(Mandatory)][string]$Section,
        [Parameter(Mandatory)][string]$Key
    )

    $currentSection = ''
    $keyPattern = '^\s*' + [regex]::Escape($Key) + '\s*=\s*(.*?)\s*$'
    foreach ($line in ($Content -split "`r?`n")) {
        if ($line -match '^\s*\[([^\]]+)\]\s*$') {
            $currentSection = $Matches[1].Trim()
            continue
        }
        if ($currentSection -ieq $Section -and $line -match $keyPattern) {
            return $Matches[1].Trim()
        }
    }
    return $null
}

function Set-IniValue {
    param(
        [Parameter(Mandatory)][string]$Content,
        [Parameter(Mandatory)][string]$Section,
        [Parameter(Mandatory)][string]$Key,
        [Parameter(Mandatory)][string]$Value
    )

    $lines = [System.Collections.Generic.List[string]]::new()
    $currentSection = ''
    $updated = $false
    $keyPattern = '^\s*' + [regex]::Escape($Key) + '\s*='
    foreach ($line in ($Content -split "`r?`n")) {
        $newLine = $line
        if ($line -match '^\s*\[([^\]]+)\]\s*$') {
            $currentSection = $Matches[1].Trim()
        } elseif ($currentSection -ieq $Section -and $line -match $keyPattern) {
            $newLine = "$Key = $Value"
            $updated = $true
        }
        $lines.Add($newLine)
    }
    if (-not $updated) {
        throw "Docker 配置缺少 [$Section] 中的 $Key 项，请检查 backend/config.ini。"
    }
    return [string]::Join([Environment]::NewLine, $lines)
}

Push-Location $projectRoot
try {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw '找不到 docker 命令。请先启动或安装 Docker Desktop。'
    }

    $engineType = (& docker info --format '{{.OSType}}' 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $engineType -ne 'linux') {
        throw 'Docker Engine 未就绪或当前使用 Windows 容器。请在 Docker Desktop 切换并启动 Linux 容器。'
    }

    $redisPing = (& docker exec redis redis-cli ping 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $redisPing -ne 'PONG' -or
        -not (Test-NetConnection -ComputerName 127.0.0.1 -Port 6379 -InformationLevel Quiet)) {
        throw '现有 Docker Redis 容器 redis 不可用，或宿主机 6379 端口未发布。请先启动该 Redis 容器并确认 redis-cli ping 返回 PONG。'
    }

    if (-not (Test-Path -LiteralPath $sourceConfigPath -PathType Leaf)) {
        throw '缺少 backend/config.ini。请从 config.ini.example 复制并填写 SQL Server 登录配置后重试。'
    }

    $listener = @(Get-NetTCPConnection -State Listen -LocalPort 5173 -ErrorAction SilentlyContinue)
    $runningFrontend = (& docker compose ps --status running -q frontend 2>$null | Out-String).Trim()
    $runningBackend = (& docker compose ps --status running -q backend 2>$null | Out-String).Trim()
    if ($listener.Count -gt 0 -and [string]::IsNullOrWhiteSpace($runningFrontend)) {
        throw '宿主机 5173 端口正在被其他进程使用。请先停止本机 Vite 前端，再重新运行此脚本。'
    }

    if (-not (Test-Path -LiteralPath $dockerConfigPath -PathType Leaf)) {
        $dockerConfigChanged = $true
        Copy-Item -LiteralPath $sourceConfigPath -Destination $dockerConfigPath
        $content = [System.IO.File]::ReadAllText($dockerConfigPath)
        $auth = Get-IniValue -Content $content -Section 'database' -Key 'authentication'
        if ($auth -ne 'sql') {
            throw 'Linux 容器需要 SQL 登录。请在 backend/config.ini 中配置 authentication = sql 及 SQL 用户名、密码。'
        }

        # 应用配置保持允许的本机地址，Dockerfile 只覆盖容器内 Uvicorn 的监听地址。
        $content = Set-IniValue -Content $content -Section 'api' -Key 'host' -Value '127.0.0.1'
        $content = Set-IniValue -Content $content -Section 'database' -Key 'server' -Value 'host.docker.internal,1433'
        $content = Set-IniValue -Content $content -Section 'redis' -Key 'host' -Value 'host.docker.internal'
        [System.IO.File]::WriteAllText($dockerConfigPath, $content, [System.Text.UTF8Encoding]::new($false))
        Write-Host '已生成本机私有容器配置 backend/config.docker.ini；该文件已加入 Git 忽略规则。'
    }

    $dockerConfig = [System.IO.File]::ReadAllText($dockerConfigPath)
    if ((Get-IniValue -Content $dockerConfig -Section 'redis' -Key 'host') -ne 'host.docker.internal') {
        # 复用 Docker Desktop 中已运行并发布 6379 端口的 Redis 容器。
        $dockerConfigChanged = $true
        $dockerConfig = Set-IniValue -Content $dockerConfig -Section 'redis' -Key 'host' -Value 'host.docker.internal'
        [System.IO.File]::WriteAllText($dockerConfigPath, $dockerConfig, [System.Text.UTF8Encoding]::new($false))
    }
    if ((Get-IniValue -Content $dockerConfig -Section 'api' -Key 'host') -notin @('127.0.0.1', 'localhost') -or
        (Get-IniValue -Content $dockerConfig -Section 'database' -Key 'server') -ne 'host.docker.internal,1433' -or
        (Get-IniValue -Content $dockerConfig -Section 'database' -Key 'authentication') -ne 'sql' -or
        (Get-IniValue -Content $dockerConfig -Section 'redis' -Key 'host') -ne 'host.docker.internal') {
        throw 'backend/config.docker.ini 的容器地址或认证配置不正确；请按部署文档修正后重试。'
    }
    if ([string]::IsNullOrWhiteSpace((Get-IniValue -Content $dockerConfig -Section 'database' -Key 'username')) -or
        [string]::IsNullOrWhiteSpace((Get-IniValue -Content $dockerConfig -Section 'database' -Key 'password'))) {
        throw 'backend/config.docker.ini 缺少 SQL Server 用户名或密码，请填写后重试。'
    }

    if (-not (Test-NetConnection -ComputerName 127.0.0.1 -Port 1433 -InformationLevel Quiet)) {
        throw '无法连接本机 TCP 1433。请先启动 SQL Server 并确认 QuantBacktest 数据库可用。'
    }

    & docker compose config --quiet
    if ($LASTEXITCODE -ne 0) {
        throw 'Compose 配置校验失败，请检查 compose.yaml。'
    }

    & docker compose up -d --build --remove-orphans
    if ($LASTEXITCODE -ne 0) {
        throw 'Docker 镜像构建或容器启动失败。请运行 docker compose logs --tail 100 查看日志。'
    }

    $restartFrontend = $false
    if ($dockerConfigChanged -and -not [string]::IsNullOrWhiteSpace($runningBackend)) {
        # bind mount 内容变更不会自动重启进程；重启一次让应用重新读取 Redis 地址。
        & docker compose restart backend
        if ($LASTEXITCODE -ne 0) {
            throw 'Docker 配置已更新，但后端重启失败。请运行 docker compose logs --tail 100 backend 查看日志。'
        }
        $restartFrontend = $true
    } elseif (-not [string]::IsNullOrWhiteSpace($runningBackend)) {
        $currentBackend = (& docker compose ps --status running -q backend 2>$null | Out-String).Trim()
        if ($runningBackend -ne $currentBackend) {
            # 后端容器重建后可能获得新 IP；重启 Nginx 以更新其上游地址解析。
            $restartFrontend = $true
        }
    }
    if ($restartFrontend) {
        & docker compose restart frontend
        if ($LASTEXITCODE -ne 0) {
            throw '后端地址已更新，但前端 Nginx 未能重启。请运行 docker compose logs --tail 100 frontend 查看日志。'
        }
    }

    $ready = $false
    for ($attempt = 0; $attempt -lt 45; $attempt++) {
        try {
            $page = Invoke-WebRequest -Uri 'http://127.0.0.1:5173/' -TimeoutSec 3 -UseBasicParsing
            $null = Invoke-RestMethod -Uri 'http://127.0.0.1:5173/api/fund-paper/strategies' -TimeoutSec 5
            $null = Invoke-RestMethod -Uri 'http://127.0.0.1:5173/api/paper-trading/sessions' -TimeoutSec 5
            $null = Invoke-RestMethod -Uri 'http://127.0.0.1:5173/api/fund-paper/sessions' -TimeoutSec 5
            if ($page.StatusCode -eq 200) {
                $ready = $true
                break
            }
        } catch {
            Start-Sleep -Seconds 2
        }
    }

    if (-not $ready) {
        & docker compose ps
        & docker compose logs --tail 80 backend frontend
        throw '容器已启动，但页面或 API 尚未通过检查。请根据上面的容器日志排查。'
    }

    Write-Host ''
    Write-Host 'QuantBacktest 前后端已在 Docker Desktop 中运行。'
    Write-Host '页面：http://localhost:5173'
    Write-Host 'API 文档：http://localhost:5173/docs'
    Write-Host '后端已成功读取股票和基金纸面实验接口（SQL Server）。'
    & docker compose ps
} finally {
    Pop-Location
}
