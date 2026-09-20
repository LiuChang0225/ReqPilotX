param(
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8000,
    [switch]$WithQdrant,
    [string]$QdrantContainerName = "reqpilotx-qdrant",
    [string]$QdrantImage = "qdrant/qdrant:latest",
    [int]$QdrantHttpPort = 6333,
    [int]$QdrantGrpcPort = 6334
)

$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RootDir
$env:PYTHONPATH = "."

if ($BindHost -ne "127.0.0.1") {
    throw "ReqPilotX local services must bind to 127.0.0.1."
}

function Get-PythonExecutable {
    if ($env:REQPILOT_PYTHON) {
        return $env:REQPILOT_PYTHON
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        return $python.Source
    }
    throw "Python executable not found. Install Python or set REQPILOT_PYTHON."
}

function Test-PortListening {
    param([int]$LocalPort)

    $connection = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue
    return $null -ne $connection
}

function Assert-LoopbackOnlyListener {
    param([int]$LocalPort)

    $listeners = @(
        Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue
    )
    $unsafeListeners = @(
        $listeners | Where-Object { $_.LocalAddress -notin @("127.0.0.1", "::1") }
    )
    if ($unsafeListeners.Count -gt 0) {
        throw "Unsafe non-loopback listener detected on port $LocalPort. Stop the old service before continuing."
    }
}

function Test-QdrantReady {
    param([int]$LocalPort)

    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:${LocalPort}/collections" -UseBasicParsing -TimeoutSec 3
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 300
    } catch {
        return $false
    }
}

function Test-QdrantContainerLoopbackBindings {
    param(
        [string]$DockerExecutable,
        [string]$ContainerName,
        [int]$HttpPort,
        [int]$GrpcPort
    )

    try {
        $bindings = (
            & $DockerExecutable inspect --format "{{json .HostConfig.PortBindings}}" $ContainerName |
                Out-String
        ).Trim() | ConvertFrom-Json
        $httpBindings = @($bindings.'6333/tcp')
        $grpcBindings = @($bindings.'6334/tcp')
        return (
            $httpBindings.Count -eq 1 -and
            $grpcBindings.Count -eq 1 -and
            $httpBindings[0].HostIp -eq "127.0.0.1" -and
            $httpBindings[0].HostPort -eq "$HttpPort" -and
            $grpcBindings[0].HostIp -eq "127.0.0.1" -and
            $grpcBindings[0].HostPort -eq "$GrpcPort"
        )
    } catch {
        return $false
    }
}

function Ensure-QdrantContainer {
    param(
        [string]$ContainerName,
        [string]$Image,
        [int]$HttpPort,
        [int]$GrpcPort
    )

    Assert-LoopbackOnlyListener -LocalPort $HttpPort
    Assert-LoopbackOnlyListener -LocalPort $GrpcPort

    if (Test-QdrantReady -LocalPort $HttpPort) {
        Write-Output "[INFO] Qdrant is already reachable: http://127.0.0.1:${HttpPort}"
        return
    }

    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $docker) {
        throw "Docker was not found. Start Docker Desktop or omit -WithQdrant."
    }

    $storageDir = Join-Path $RootDir "storage\qdrant"
    New-Item -ItemType Directory -Force -Path $storageDir | Out-Null
    $resolvedStorageDir = (Resolve-Path $storageDir).Path

    $existingContainer = (& $docker.Source ps -a --filter "name=^/$ContainerName$" --format "{{.Names}}" | Out-String).Trim()
    if (
        $existingContainer -eq $ContainerName -and
        -not (Test-QdrantContainerLoopbackBindings -DockerExecutable $docker.Source -ContainerName $ContainerName -HttpPort $HttpPort -GrpcPort $GrpcPort)
    ) {
        throw "Existing Qdrant container has unsafe port mappings. Remove '$ContainerName' and start again."
    }

    $runningContainer = (& $docker.Source ps --filter "name=^/$ContainerName$" --format "{{.Names}}" | Out-String).Trim()
    if ($runningContainer -eq $ContainerName) {
        Write-Output "[INFO] Qdrant container already running: $ContainerName"
    } else {
        if ($existingContainer -eq $ContainerName) {
            Write-Output "[INFO] Starting existing Qdrant container: $ContainerName"
            & $docker.Source start $ContainerName | Out-Null
        } else {
            Write-Output "[INFO] Creating and starting Qdrant container: $ContainerName"
            & $docker.Source run -d --name $ContainerName -p "127.0.0.1:${HttpPort}:6333" -p "127.0.0.1:${GrpcPort}:6334" -v "${resolvedStorageDir}:/qdrant/storage" $Image | Out-Null
        }
    }

    try {
        Assert-LoopbackOnlyListener -LocalPort $HttpPort
        Assert-LoopbackOnlyListener -LocalPort $GrpcPort
    } catch {
        & $docker.Source stop $ContainerName | Out-Null
        throw
    }

    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if (Test-QdrantReady -LocalPort $HttpPort) {
            Write-Output "[OK] Qdrant is ready: http://127.0.0.1:${HttpPort}"
            return
        }
        Start-Sleep -Milliseconds 500
    }

    throw "Qdrant startup timed out. Check docker logs $ContainerName."
}

if ($WithQdrant) {
    Ensure-QdrantContainer -ContainerName $QdrantContainerName -Image $QdrantImage -HttpPort $QdrantHttpPort -GrpcPort $QdrantGrpcPort
}

if (Test-PortListening -LocalPort $Port) {
    throw "Port $Port is already in use. Stop the current service or choose a different -Port."
}

$pythonExe = Get-PythonExecutable
Write-Output "[INFO] Starting ReqPilotX: http://${BindHost}:${Port}"
& $pythonExe -m uvicorn app.main:app --host $BindHost --port $Port
