param([string]$EnvironmentDir = ".venv-qwen")
$ErrorActionPreference = "Stop"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  throw "uv is required. Install it from https://docs.astral.sh/uv/ and retry."
}

uv venv --python 3.12 $EnvironmentDir
if ($LASTEXITCODE -ne 0) { throw "Could not create the Qwen3-TTS environment." }

$pythonPath = Join-Path $EnvironmentDir "Scripts\python.exe"
uv pip install --python $pythonPath -e ".[test]" qwen-tts soundfile
if ($LASTEXITCODE -ne 0) { throw "Qwen3-TTS dependency installation failed." }

Write-Host "Qwen3-TTS dependencies are ready."
Write-Host "Start with: $pythonPath -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
Write-Host "The model weights are downloaded and cached on first generation."
