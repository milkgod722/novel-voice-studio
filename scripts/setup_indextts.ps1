param([string]$InstallDir = "IndexTTS2")
$ErrorActionPreference = "Stop"
if (-not (Get-Command git-lfs -ErrorAction SilentlyContinue)) {
  throw "Git LFS is required. Run: winget install GitHub.GitLFS"
}
git lfs install
if ($LASTEXITCODE -ne 0) { throw "git lfs install failed." }
if (-not (Test-Path -LiteralPath $InstallDir)) {
  git clone --depth 1 https://github.com/index-tts/index-tts.git $InstallDir
  if ($LASTEXITCODE -ne 0) {
    Write-Host "GitHub clone failed; falling back to the official source archive."
    $archivePath = Join-Path (Get-Location) "indextts-main.zip"
    curl.exe -L --fail --retry 2 https://codeload.github.com/index-tts/index-tts/zip/refs/heads/main -o $archivePath
    if ($LASTEXITCODE -ne 0) { throw "Could not download the IndexTTS2 source archive." }
    Expand-Archive -LiteralPath $archivePath -DestinationPath (Get-Location)
    Move-Item -LiteralPath (Join-Path (Get-Location) "index-tts-main") -Destination $InstallDir
  }
}
Push-Location $InstallDir
try {
  if (Test-Path -LiteralPath ".git") {
    git lfs pull
    if ($LASTEXITCODE -ne 0) { throw "git lfs pull failed." }
  }
  # Skip difficult Windows extras such as DeepSpeed and flash-attn.
  uv sync --extra webui --default-index "https://mirrors.aliyun.com/pypi/simple"
  if ($LASTEXITCODE -ne 0) { throw "IndexTTS2 dependency installation failed." }
  uv tool install modelscope --default-index "https://mirrors.aliyun.com/pypi/simple"
  if ($LASTEXITCODE -ne 0) { throw "ModelScope CLI installation failed." }
  $toolBin = uv tool dir --bin
  & (Join-Path $toolBin "modelscope.exe") download --model IndexTeam/IndexTTS-2 --local_dir checkpoints
  if ($LASTEXITCODE -ne 0) { throw "IndexTTS2 model download failed." }
  Write-Host "IndexTTS2 is ready. See README.md for the real-engine command."
} finally { Pop-Location }
