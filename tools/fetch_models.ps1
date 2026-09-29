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

# sha256 取自 GitHub release 资产的 digest。镜像（包括 ModelScope）上的同名文件不一定是同一份 ——
# 实测 ModelScope 的 funasr-nano-int8-2025-12-30 比官方小 91MB，curl 却不报错。校验不过就删掉重下。
# funasr-nano-llm 是默认模型（带 Qwen3-0.6B 解码器，能结合上下文和热词），约 840MB
$items = @(
  @{ tag='asr-models'; file='sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09.tar.bz2'; dir='sense-voice';     marker='model.int8.onnx';
     sha256='7305f7905bfcf77fa0b39388a313f3da35c68d971661a65475b56fb2162c8e63' },
  @{ tag='asr-models'; file='sherpa-onnx-sense-voice-funasr-nano-int8-2025-12-17.tar.bz2';     dir='funasr-nano';     marker='model.int8.onnx';
     sha256='257936ea9a64cbe33200274e6367fc26d373ff6ca58b996b15108ffd6b9f6148' },
  @{ tag='asr-models'; file='sherpa-onnx-funasr-nano-int8-2025-12-30.tar.bz2';                 dir='funasr-nano-llm'; marker='llm.int8.onnx';
     sha256='eb43d7ccc2e86b243f6a03b7df361033dda66db9523d1a92bf6aca2b50c9476b' },
  @{ tag='asr-models'; file='silero_vad.onnx';                                                 dir=$null;             marker=$null;
     sha256='9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6' }
)

foreach ($it in $items) {
  if ($it.dir -and (Test-Path (Join-Path (Join-Path $models $it.dir) $it.marker))) {
    Write-Host "[skip] $($it.dir) already installed"; continue      # 已解压就不必再下几百 MB
  }
  $dest = Join-Path $tmp $it.file
  if ((Test-Path $dest) -and (Get-FileHash $dest -Algorithm SHA256).Hash -ne $it.sha256.ToUpper()) {
    Write-Host "[bad-hash] $($it.file) -- redownload"
    Remove-Item $dest -Force
  }
  if (Test-Path $dest) { Write-Host "[skip-dl] $($it.file)"; }
  else {
    $ok = $false
    foreach ($b in $bases) {
      $url = "$b/$($it.tag)/$($it.file)"
      try {
        Write-Host "[get] $url"
        & curl.exe -L --fail --retry 2 --connect-timeout 20 -o "$dest" "$url"
        if ($LASTEXITCODE -eq 0 -and (Test-Path $dest)) {
          if ((Get-FileHash $dest -Algorithm SHA256).Hash -eq $it.sha256.ToUpper()) { $ok = $true; break }
          Write-Host "[bad-hash] $url"
          Remove-Item $dest -Force
        }
      } catch { Write-Host "[warn] $($_.Exception.Message)" }
    }
    if (-not $ok) { throw "download failed or sha256 mismatch: $($it.file)" }
  }

  if ($it.dir) {
    $target = Join-Path $models $it.dir
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
