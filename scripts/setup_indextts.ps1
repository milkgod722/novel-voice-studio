param([string]$InstallDir = "IndexTTS2")
$ErrorActionPreference = "Stop"
if (-not (Get-Command git-lfs -ErrorAction SilentlyContinue)) {
  throw "Git LFS is required. Run: winget install GitHub.GitLFS"
}
git lfs install
if (-not (Test-Path -LiteralPath $InstallDir)) {
  git clone https://github.com/index-tts/index-tts.git $InstallDir
}
Push-Location $InstallDir
try {
  git lfs pull
  # Skip difficult Windows extras such as DeepSpeed and flash-attn.
  uv sync --extra webui
  uv tool install "huggingface-hub[cli,hf_xet]"
  $toolBin = uv tool dir --bin
  & (Join-Path $toolBin "hf.exe") download IndexTeam/IndexTTS-2 --local-dir=checkpoints
  Write-Host "IndexTTS2 is ready. See README.md for the real-engine command."
} finally { Pop-Location }
