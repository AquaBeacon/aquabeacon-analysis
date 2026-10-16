param(
    [switch]$Build,
    [switch]$Pilot,
    [string]$ExperimentId,
    [string]$SettingsPath,
    [string]$ImuOverridePath,
    [string]$DatasetRoot = 'data/interim/orbslam3',
    [string]$RunName,
    [ValidateSet('MonocularInertial', 'Monocular')][string]$SensorMode = 'MonocularInertial',
    [switch]$RequireMapExport,
    [int]$StartImage = -1,
    [int]$EndImage = -1
)

$ErrorActionPreference = 'Stop'
$repository = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
if ($dockerCommand) {
    $dockerPath = $dockerCommand.Source
} else {
    $dockerPath = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe'
    if (-not (Test-Path -LiteralPath $dockerPath)) {
        throw 'Docker is unavailable. Install WSL 2 and Docker Desktop before building ORB-SLAM3.'
    }
}
$env:PATH = "$(Split-Path -Parent $dockerPath);$env:PATH"

if ($Build) {
    $buildContext = Join-Path $repository 'docker/orbslam3'
    & $dockerPath build -f (Join-Path $buildContext 'Dockerfile') -t aquabeacon-orbslam3:4452a3c $buildContext
    if ($LASTEXITCODE -ne 0) { throw 'ORB-SLAM3 Docker build failed.' }
    $binaryDir = Join-Path $repository 'data/interim/orbslam3_bin'
    New-Item -ItemType Directory -Force -Path $binaryDir | Out-Null
    $containerId = (& $dockerPath create aquabeacon-orbslam3:4452a3c).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Could not create ORB-SLAM3 image container.' }
    try {
        & $dockerPath cp "${containerId}:/opt/ORB_SLAM3/Examples/Monocular/mono_euroc" (Join-Path $binaryDir 'mono_euroc')
        if ($LASTEXITCODE -ne 0) { throw 'Could not copy mono_euroc from image.' }
    } finally {
        & $dockerPath rm $containerId | Out-Null
    }
}

if ($ExperimentId) {
    if ($RequireMapExport -and $SensorMode -ne 'Monocular') {
        throw 'MapPoint PLY export is available only for monocular mode.'
    }
    if (-not $SettingsPath) { throw 'An ORB-SLAM3 settings YAML is required.' }
    $settings = (Resolve-Path -LiteralPath $SettingsPath).Path
    $dataset = (Resolve-Path -LiteralPath (Join-Path $repository "$DatasetRoot/$ExperimentId")).Path
    $timestamps = Join-Path $dataset 'timestamps.txt'
    if (-not (Test-Path -LiteralPath $timestamps)) { throw "Missing $timestamps" }
    $manifest = Get-Content -LiteralPath (Join-Path $dataset 'export_manifest.json') -Raw | ConvertFrom-Json
    if (-not $manifest.frame_alignment_note) {
        throw 'Frame alignment is undocumented.'
    }
    if ($manifest.test_only -and -not $Pilot) {
        throw 'Use -Pilot with --max-frames exports.'
    }
    $timestampsInput = $timestamps
    if ($StartImage -ge 0 -or $EndImage -ge 0) {
        if (-not $Pilot -or $StartImage -lt 0 -or $EndImage -le $StartImage) {
            throw 'A segment needs -Pilot and 0 <= StartImage < EndImage.'
        }
        $allTimes = @(Get-Content -LiteralPath $timestamps)
        if ($EndImage -gt $allTimes.Count) { throw 'EndImage exceeds image count.' }
        $timestampsInput = Join-Path $dataset "timestamps_pilot_${StartImage}_${EndImage}.txt"
        $lines = $allTimes[$StartImage..($EndImage - 1)]
        [System.IO.File]::WriteAllText($timestampsInput, (($lines -join "`n") + "`n"))
    } elseif ($Pilot -and -not $manifest.test_only) {
        throw 'Full exports need StartImage and EndImage for -Pilot.'
    }
    $outputName = if ($StartImage -ge 0) { "pilot_orbslam3_${StartImage}_${EndImage}" } `
        elseif ($Pilot) { 'pilot_orbslam3_raw' } else { 'orbslam3_raw' }
    $imuOverride = $null
    if ($ImuOverridePath) {
        $imuOverride = (Resolve-Path -LiteralPath $ImuOverridePath).Path
        $outputName = "fused_$outputName"
    }
    if ($RunName) {
        if ($RunName -notmatch '^[A-Za-z0-9][A-Za-z0-9_-]*$') {
            throw 'RunName must contain only letters, digits, underscores, and hyphens.'
        }
        $outputName = $RunName
    }
    $output = Join-Path $repository "results/$ExperimentId/vislam_depth/$outputName"
    if (Test-Path -LiteralPath (Join-Path $output 'run_manifest.json')) {
        throw "Run output already exists: $output"
    }
    New-Item -ItemType Directory -Force -Path $output | Out-Null
    $settingsHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $settings).Hash.ToLowerInvariant()
    $cameraCalibrationSource = $null
    $cameraCalibrationHash = $null
    if ((Split-Path -Leaf $settings) -match 'underwater_trial') {
        $cameraCalibrationSource = 'calibration/parent_camera_underwater_20260924_provisional.json'
        $cameraCalibrationHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $repository $cameraCalibrationSource)).Hash.ToLowerInvariant()
        if (-not (Select-String -LiteralPath $settings -Pattern $cameraCalibrationHash -Quiet)) {
            throw 'Trial settings are stale relative to the underwater calibration JSON. Regenerate them first.'
        }
    }
    $runMetadata = @{
        experiment_id = $ExperimentId
        sensor_mode = $SensorMode
        test_only = [bool]$Pilot
        calibration_status = if ((Split-Path -Leaf $settings) -match 'provisional|fused_trial|underwater_trial') { 'provisional' } else { 'user_supplied' }
        settings_path = $settings
        settings_sha256 = $settingsHash
        export_manifest = (Join-Path $dataset 'export_manifest.json')
        timestamps_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $timestampsInput).Hash.ToLowerInvariant()
        start_image = $StartImage
        end_image_exclusive = $EndImage
        git_commit = (git -C $repository rev-parse HEAD).Trim()
        imu_override_path = $imuOverride
        imu_override_sha256 = if ($imuOverride) { (Get-FileHash -Algorithm SHA256 -LiteralPath $imuOverride).Hash.ToLowerInvariant() } else { $null }
        imu_override_manifest = if ($imuOverride) { Join-Path (Split-Path -Parent $imuOverride) 'manifest.json' } else { $null }
        image_processing = if ($manifest.clahe_clip_limit) { "CLAHE clipLimit=$($manifest.clahe_clip_limit) tileGrid=8x8" } else { 'none' }
        source_export_manifest_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $dataset 'export_manifest.json')).Hash.ToLowerInvariant()
        input_image_extension = if ($manifest.image_extension) { $manifest.image_extension } else { '.png' }
        input_color_channels = if ($manifest.color_channels) { $manifest.color_channels } else { 1 }
        camera_calibration_source = $cameraCalibrationSource
        camera_calibration_sha256 = $cameraCalibrationHash
    }
    $runMetadata | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $output 'run_manifest.json')
    $dockerArgs = @('run', '--rm', '--mount', "type=bind,source=$dataset,target=/dataset,readonly")
    if ($SensorMode -eq 'Monocular' -and $imuOverride) {
        throw 'IMU override is not applicable to monocular mode.'
    }
    if ($imuOverride) {
        $dockerArgs += @('--mount', "type=bind,source=$imuOverride,target=/dataset/mav0/imu0/data.csv,readonly")
    }
    $dockerArgs += @('--mount', "type=bind,source=$settings,target=/settings.yaml,readonly",
        '--mount', "type=bind,source=$output,target=/work")
    if ($manifest.image_extension) {
        if ($manifest.image_extension -notin @('.png', '.jpg')) {
            throw "Unsupported input image extension: $($manifest.image_extension)"
        }
        $dockerArgs += @('--env', "ORB_IMAGE_EXTENSION=$($manifest.image_extension)")
    }
    if ($SensorMode -eq 'Monocular') {
        $binary = Join-Path $repository 'data/interim/orbslam3_bin/mono_euroc'
        if (-not (Test-Path -LiteralPath $binary)) {
            throw 'Build mono_euroc into data/interim/orbslam3_bin before monocular execution.'
        }
        $dockerArgs += @('--mount', "type=bind,source=$binary,target=/mono_euroc,readonly")
        $runMetadata.binary_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $binary).Hash.ToLowerInvariant()
        $executable = '/mono_euroc'
    } else {
        $executable = '/opt/ORB_SLAM3/Examples/Monocular-Inertial/mono_inertial_euroc'
    }
    $dockerArgs += @('aquabeacon-orbslam3:4452a3c', $executable,
        '/opt/ORB_SLAM3/Vocabulary/ORBvoc.txt', '/settings.yaml', '/dataset',
        "/dataset/$(Split-Path -Leaf $timestampsInput)")
    & $dockerPath @dockerArgs 2>&1 | Tee-Object -FilePath (Join-Path $output 'run.log')
    $runMetadata.exit_code = $LASTEXITCODE
    $trajectoryPath = Join-Path $output 'CameraTrajectory.txt'
    $runMetadata.trajectory_rows = if (Test-Path -LiteralPath $trajectoryPath) {
        @(Get-Content -LiteralPath $trajectoryPath).Count
    } else { 0 }
    $runMetadata.inertial_initialization_evidence = [bool](
        Select-String -LiteralPath (Join-Path $output 'run.log') `
            -Pattern 'Global Bundle Adjustment finished' -Quiet)
    $runMetadata.usable_vi_trajectory = ($LASTEXITCODE -eq 0 -and
        $SensorMode -eq 'MonocularInertial' -and $runMetadata.trajectory_rows -ge 10 -and
        $runMetadata.inertial_initialization_evidence)
    $runMetadata.visual_trajectory_rows = $runMetadata.trajectory_rows
    $runMetadata.usable_visual_trajectory = ($LASTEXITCODE -eq 0 -and
        $runMetadata.trajectory_rows -ge 10)
    $mapIndexPath = Join-Path $output 'MapPoints_manifest.csv'
    $runMetadata.map_export_available = Test-Path -LiteralPath $mapIndexPath
    $runMetadata.map_export_patch_sha256 = if ($runMetadata.map_export_available) {
        (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $repository 'docker/orbslam3/export_map_points.patch')).Hash.ToLowerInvariant()
    } else { $null }
    $runMetadata.map_count_exported = if ($runMetadata.map_export_available) {
        @(Import-Csv -LiteralPath $mapIndexPath).Count
    } else { 0 }
    $featureIndexPath = Join-Path $output 'TrackedFrameSummary.csv'
    $runMetadata.feature_export_available = Test-Path -LiteralPath $featureIndexPath
    $runMetadata.feature_export_patch_sha256 = if ($runMetadata.feature_export_available) {
        (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $repository 'docker/orbslam3/export_tracked_features.patch')).Hash.ToLowerInvariant()
    } else { $null }
    $runMetadata | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $output 'run_manifest.json')
    if ($LASTEXITCODE -ne 0) { throw "ORB-SLAM3 execution failed with exit code $LASTEXITCODE." }
    if ($SensorMode -eq 'MonocularInertial' -and -not $runMetadata.usable_vi_trajectory) {
        throw 'ORB-SLAM3 exited but did not produce a verified inertial VI trajectory.'
    }
    if ($SensorMode -eq 'Monocular' -and -not $runMetadata.usable_visual_trajectory) {
        throw 'ORB-SLAM3 exited but did not produce a usable monocular trajectory.'
    }
    if ($RequireMapExport -and $runMetadata.map_count_exported -lt 1) {
        throw 'ORB-SLAM3 did not export a 3D MapPoint PLY.'
    }
    Write-Output $output
}
