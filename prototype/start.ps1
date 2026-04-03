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

function Test-QdrantReady {
    param([int]$LocalPort)

    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:${LocalPort}/collections" -UseBasicParsing -TimeoutSec 3
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 300
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

    $runningContainer = (& $docker.Source ps --filter "name=^/$ContainerName$" --format "{{.Names}}" | Out-String).Trim()
    if ($runningContainer -eq $ContainerName) {
        Write-Output "[INFO] Qdrant container already running: $ContainerName"
    } else {
        $existingContainer = (& $docker.Source ps -a --filter "name=^/$ContainerName$" --format "{{.Names}}" | Out-String).Trim()
        if ($existingContainer -eq $ContainerName) {
            Write-Output "[INFO] Starting existing Qdrant container: $ContainerName"
            & $docker.Source start $ContainerName | Out-Null
        } else {
            Write-Output "[INFO] Creating and starting Qdrant container: $ContainerName"
            & $docker.Source run -d --name $ContainerName -p "${HttpPort}:6333" -p "${GrpcPort}:6334" -v "${resolvedStorageDir}:/qdrant/storage" $Image | Out-Null
        }
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