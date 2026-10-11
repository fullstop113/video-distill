# 可续传的大文件下载器。
#
# 教训（P0 实测记录，见 docs/p0-findings.md §1.3）：
# curl 的速度下限开关会在慢速时**主动放弃**传输。对 2.5GB 的 torch wheel，
# 它会在某个位置停下来留下半成品，让人误判成"下载卡死/镜像有问题"。
# 所以这里**不传任何速度下限参数**，改用「Content-Length 校验 + 续传重试」。
# 验收测试会断言：传给 curl 的参数里不出现速度下限开关。

param(
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)][string]$Out
)

$ErrorActionPreference = 'Continue'
$dir = Split-Path -Parent $Out
New-Item -ItemType Directory -Force -Path $dir | Out-Null

# 取总长。必须取**所有** content-length 里最大的那个：
# 走 301 重定向时第一个响应头是 Content-Length: 0，若只取第一个匹配，
# $expected 会恒为 0，长度校验形同虚设（这是真实踩过的坑）。
$expected = 0
try {
    $head = & curl.exe -4 -sIL --max-time 30 $Url
    foreach ($m in [regex]::Matches(($head -join "`n"), 'content-length:\s*(\d+)', 'IgnoreCase')) {
        $v = [int64]$m.Groups[1].Value
        if ($v -gt $expected) { $expected = $v }
    }
} catch { }

if ($expected -eq 0) {
    Write-Output "WARN 未能取得 Content-Length，将只做「有进展」判断"
}
Write-Output "TARGET $(Split-Path $Out -Leaf) expected=$expected bytes"

$curlArgs = @(
    '-4', '-L', '--fail', '--retry', '5', '--retry-all-errors', '--retry-delay', '3',
    '-C', '-', '--connect-timeout', '20', '-sS'
)

for ($i = 1; $i -le 20; $i++) {
    $size = if (Test-Path $Out) { (Get-Item $Out).Length } else { 0 }
    if ($expected -gt 0 -and $size -eq $expected) { break }
    Write-Output "=== attempt $i $(Get-Date -Format HH:mm:ss) have=$size / $expected ==="
    & curl.exe @curlArgs -o $Out $Url
    $code = $LASTEXITCODE
    $now = if (Test-Path $Out) { (Get-Item $Out).Length } else { 0 }
    Write-Output "curl exit=$code size=$now"
    if ($expected -gt 0 -and $now -eq $expected) { Write-Output 'COMPLETE'; break }
    if ($code -eq 0 -and $now -gt 0 -and $now -eq $size) { Write-Output 'NO_PROGRESS'; break }
    Start-Sleep -Seconds 3
}

$final = if (Test-Path $Out) { (Get-Item $Out).Length } else { 0 }
if ($expected -gt 0 -and $final -eq $expected) {
    Write-Output "OK $Out ($([math]::Round($final / 1MB, 1)) MB)"
    exit 0
}
Write-Output "INCOMPLETE $Out ($final / $expected bytes)"
exit 1
