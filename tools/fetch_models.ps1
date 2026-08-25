$ErrorActionPreference = 'Stop'
$root   = Split-Path -Parent $PSScriptRoot
$models = Join-Path $root "models"
$tmp    = Join-Path $root "models\_dl"
New-Item -ItemType Directory -Force -Path $models, $tmp | Out-Null

# GitHub releases 在本机 PowerShell 可直连；ghproxy 作为降级镜像（§5 fallback）
$bases = @(
  "https://github.com/k2-fsa/sherpa-onnx/releases/download",
  "https://ghproxy.net/https://github.com/k2-fsa/sherpa-onnx/releases/download"
)

$items = @(
  @{ tag='asr-models'; file='sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09.tar.bz2'; dir='sense-voice' },
  @{ tag='asr-models'; file='sherpa-onnx-sense-voice-funasr-nano-int8-2025-12-17.tar.bz2';     dir='funasr-nano' },
  @{ tag='asr-models'; file='silero_vad.onnx';                                                 dir=$null }
)

foreach ($it in $items) {
  $dest = Join-Path $tmp $it.file
  if (Test-Path $dest) { Write-Host "[skip-dl] $($it.file)"; }
  else {
    $ok = $false
    foreach ($b in $bases) {
      $url = "$b/$($it.tag)/$($it.file)"
      try {
        Write-Host "[get] $url"
        & curl.exe -L --fail --retry 2 --connect-timeout 20 -o "$dest" "$url"
        if ($LASTEXITCODE -eq 0 -and (Test-Path $dest)) { $ok = $true; break }
      } catch { Write-Host "[warn] $($_.Exception.Message)" }
    }
    if (-not $ok) { throw "download failed: $($it.file)" }
  }

  if ($it.dir) {
    $target = Join-Path $models $it.dir
    if (Test-Path (Join-Path $target 'model.int8.onnx')) { Write-Host "[skip-ex] $($it.dir)"; continue }
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    Write-Host "[extract] $($it.file) -> $target"
    & tar.exe -xjf "$dest" -C "$target" --strip-components=1
    if ($LASTEXITCODE -ne 0) { throw "extract failed: $($it.file)" }
  } else {
    Copy-Item $dest (Join-Path $models $it.file) -Force
  }
}
Write-Host "[done] models ready"
Get-ChildItem $models -Recurse -File | Where-Object { $_.Length -gt 100KB } |
  Select-Object @{n='path';e={$_.FullName.Replace($models,'')}}, @{n='MB';e={[math]::Round($_.Length/1MB,1)}} |
  Format-Table -AutoSize
