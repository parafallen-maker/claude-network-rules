# Shared Windows runtime safeguards. No work is performed when this file is dot-sourced.
$script:OperationLockDepth = 0
$script:OperationLockHandle = $null

function Invoke-WithOperationLock([scriptblock]$Action) {
    if ($script:OperationLockDepth -gt 0) { & $Action; return }
    $lockPath = Join-Path ([IO.Path]::GetFullPath($BASE)) 'state.lock'
    if (-not (Test-NoReparsePath $lockPath)) { throw '状态锁路径包含重解析点' }
    New-Item -ItemType Directory -Path $BASE -Force -ErrorAction Stop | Out-Null
    $handle = $null
    try {
        try { $handle = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
        catch [IO.IOException] { throw '另一个切换、快照或更新操作正在运行，或无法取得状态锁' }
        $script:OperationLockHandle = $handle
        $script:OperationLockDepth = 1
        & $Action
    } finally {
        $script:OperationLockDepth = 0
        $script:OperationLockHandle = $null
        if ($handle) { $handle.Dispose() }
    }
}

function Get-AuditDecision([object[]]$Lines, $Config) {
    $all = @('代理内核','TUN网卡','出口IP','IP一致','时区','地区','IPv6','DNS','WebRTC','保镖','规则置顶','客户端语言','ASN属性','生态可达性')
    $optional = @('IP一致','DNS','WebRTC','客户端语言','ASN属性','生态可达性')
    $states = @{}
    foreach ($line in $Lines) {
        if ([string]::IsNullOrWhiteSpace([string]$line) -or [string]$line -match '^#') { continue }
        if ([string]$line -notmatch '^([^=]+)=(ok|warn|fail|skip)(?::.*)?$') {
            return @{ ok = $false; reason = '检查返回了无效输出' }
        }
        $name = $Matches[1]; $state = $Matches[2]
        if ($all -notcontains $name -or $states.ContainsKey($name)) {
            return @{ ok = $false; reason = '检查包含未知项或重复项' }
        }
        $states[$name] = $state
    }
    if ($states.Count -ne $all.Count) { return @{ ok = $false; reason = '检查输出不完整' } }
    foreach ($name in $all) {
        $state = $states[$name]
        if ($optional -notcontains $name -and $state -ne 'ok') {
            return @{ ok = $false; reason = "硬性检查未通过:$name" }
        }
        if ($state -eq 'fail') { return @{ ok = $false; reason = "检查未通过:$name" } }
        if ($state -eq 'skip' -and @($Config.disabled) -notcontains $name) {
            return @{ ok = $false; reason = "检查未经配置被跳过:$name" }
        }
    }
    return @{ ok = $true; reason = '' }
}

function Test-LaunchAudit {
    try {
        $config = Get-CheckConfig
        $lines = @(Invoke-CheckDetail)
        $decision = Get-AuditDecision $lines $config
        if (-not $decision.ok) { $script:SwitchError = $decision.reason; return $false }
        return $true
    } catch { $script:SwitchError = '无法完成启动前环境审计'; return $false }
}

function Write-DurableJson([string]$Path, $Value) {
    if (-not (Test-NoReparsePath $Path)) { throw '事务日志路径包含重解析点' }
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $parent -Force -ErrorAction Stop | Out-Null
    $temp = Join-Path $parent ('.journal-' + [guid]::NewGuid().ToString('N'))
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes(($Value | ConvertTo-Json -Depth 5 -Compress))
        $stream = [IO.File]::Open($temp, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
        if ([IO.File]::Exists($Path)) { [IO.File]::Replace($temp, $Path, [NullString]::Value) }
        else { [IO.File]::Move($temp, $Path) }
    } finally { if ([IO.File]::Exists($temp)) { [IO.File]::Delete($temp) } }
}

function Get-SwitchJournalPath { return (Join-Path $BASE 'staging/switch-journal.json') }

function Write-AtomicFile([string]$Path, [byte[]]$Bytes) {
    if (-not (Test-NoReparsePath $Path)) { throw '组件路径包含重解析点' }
    $temp = Join-Path (Split-Path -Parent $Path) ('.component-' + [guid]::NewGuid().ToString('N'))
    try {
        $stream = [IO.File]::Open($temp, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $stream.Write($Bytes,0,$Bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
        if ([IO.File]::Exists($Path)) { [IO.File]::Replace($temp,$Path,[NullString]::Value) }
        else { [IO.File]::Move($temp,$Path) }
    } finally { if ([IO.File]::Exists($temp)) { [IO.File]::Delete($temp) } }
}

function Get-UpdateJournalPath { return (Join-Path $BASE 'staging/update-journal.json') }

function Assert-NoPendingUpdate {
    if (Test-Path -LiteralPath (Get-UpdateJournalPath)) { throw '存在中断的更新，请先运行 recover；保留全部备份' }
}

function Recover-UpdateJournal {
    $path = Get-UpdateJournalPath
    if (-not (Test-Path -LiteralPath $path)) { return $false }
    if (-not (Test-NoReparsePath $path)) { throw '更新日志路径包含重解析点' }
    $record = [IO.File]::ReadAllText($path) | ConvertFrom-Json -ErrorAction Stop
    $names = @('claude-switch.ps1','claude-bodyguard.ps1','SwitcherUI.ps1','RuntimeSafety.ps1','version.txt')
    $backup = [IO.Path]::GetFullPath([string]$record.backup)
    if ([int]$record.version -ne 1 -or [string]$record.phase -notin @('applying','committed') -or
        (Split-Path -Parent $backup) -ne [IO.Path]::GetFullPath($BASE).TrimEnd([IO.Path]::DirectorySeparatorChar) -or
        (Split-Path -Leaf $backup) -notmatch '^backup-[A-Za-z0-9._-]+$' -or
        -not (Test-NoReparsePath $backup) -or @($record.files).Count -ne $names.Count) { throw '无法验证更新恢复日志' }
    $seen = @{}
    # Verify every old component before restoring any file.
    foreach ($file in $record.files) {
        $name = [string]$file.name
        if ($names -notcontains $name -or $seen.ContainsKey($name) -or $file.existed -isnot [bool]) { throw '更新恢复文件列表无效' }
        $seen[$name] = $true
        if (-not (Test-NoReparsePath (Join-Path $BASE $name))) { throw '组件目标包含重解析点' }
        if ($file.existed) {
            $source = Join-Path $backup $name
            if (-not (Test-NoReparsePath $source) -or -not [IO.File]::Exists($source) -or
                [string]$file.sha256 -notmatch '^[a-fA-F0-9]{64}$' -or
                (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash -ine [string]$file.sha256) { throw '更新备份缺失或校验不符，保留现场' }
        }
    }
    if ([string]$record.phase -eq 'committed') { [IO.File]::Delete($path); return $false }
    if (-not (Stop-ClaudeApp)) { throw 'Claude 未能退出，无法安全恢复组件' }
    $task = Get-ScheduledTask -TaskName 'ClaudeBodyguard' -ErrorAction SilentlyContinue
    if ($task) { Stop-ScheduledTask -TaskName 'ClaudeBodyguard' -ErrorAction Stop }
    foreach ($file in $record.files) {
        $destination = Join-Path $BASE ([string]$file.name)
        if ($file.existed) { Write-AtomicFile $destination ([IO.File]::ReadAllBytes((Join-Path $backup ([string]$file.name)))) }
        elseif ([IO.File]::Exists($destination)) { [IO.File]::Delete($destination) }
    }
    if ($task) { Start-ScheduledTask -TaskName 'ClaudeBodyguard' -ErrorAction Stop }
    [IO.File]::Delete($path)
    return $true
}

function Test-NoReparsePath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    while ($full) {
        if (Test-Path -LiteralPath $full) {
            $item = Get-Item -LiteralPath $full -Force -ErrorAction Stop
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { return $false }
        }
        $next = Split-Path -Parent $full
        if (-not $next -or $next -eq $full) { break }
        $full = $next
    }
    return $true
}

function Recover-SwitchJournal {
    $path = Get-SwitchJournalPath
    if (-not (Test-Path -LiteralPath $path)) { return $true }
    try {
        if (-not (Test-NoReparsePath $path)) { throw '事务日志路径包含重解析点' }
        $j = [IO.File]::ReadAllText($path) | ConvertFrom-Json -ErrorAction Stop
        if ([int]$j.version -ne 1 -or [string]$j.target -notmatch '^[A-D]$' -or
            [string]$j.previous -notmatch '^(none|[A-D])$' -or
            [string]$j.phase -notin @('prepared','old_moved','live_installed','committed')) { throw '事务日志格式无效' }
        $expected = @{ data = $DATA; old = (Get-SwitchPreviousPath); stage = (Get-SwitchStagePath) }
        foreach ($key in @('data','old','stage')) {
            if ([IO.Path]::GetFullPath([string]$j.$key) -ne [IO.Path]::GetFullPath([string]$expected[$key]) -or
                -not (Test-NoReparsePath ([string]$j.$key))) { throw '事务路径无法验证' }
        }
        $current = if (Test-Path -LiteralPath (Join-Path $BASE 'current.txt')) { [IO.File]::ReadAllText((Join-Path $BASE 'current.txt')).Trim() } else { 'none' }
        if ($current -notin @([string]$j.previous, [string]$j.target)) { throw '当前账号与事务记录不一致，保留事务现场' }
        if ($current -eq [string]$j.target -and (Test-Path -LiteralPath $DATA) -and
            ([string]$j.phase -eq 'committed' -or [string]$j.previous -ne [string]$j.target)) {
            if (Test-Path -LiteralPath ([string]$j.old)) { Register-OwnedSwitchCleanup ([string]$j.old) ([string]$j.target) }
            [IO.File]::Delete($path)
            return $true
        }
        # Preserve all uncertain new data. Only move the journal-owned old directory back.
        $recovery = Join-Path $BASE ('recovery/' + [guid]::NewGuid().ToString('N'))
        if (Test-Path -LiteralPath ([string]$j.old)) {
            New-Item -ItemType Directory -Path $recovery -Force -ErrorAction Stop | Out-Null
            if (Test-Path -LiteralPath $DATA) {
                Move-Item -LiteralPath $DATA -Destination (Join-Path $recovery 'candidate') -ErrorAction Stop
            }
            Move-Item -LiteralPath ([string]$j.old) -Destination $DATA -ErrorAction Stop
        } elseif (-not [bool]$j.had_live -and (Test-Path -LiteralPath $DATA)) {
            New-Item -ItemType Directory -Path $recovery -Force -ErrorAction Stop | Out-Null
            Move-Item -LiteralPath $DATA -Destination (Join-Path $recovery 'candidate') -ErrorAction Stop
        } elseif ([bool]$j.had_live -and -not (Test-Path -LiteralPath $DATA)) {
            throw '原目录与恢复目录均缺失，保留事务现场'
        } elseif ([bool]$j.had_live -and [string]$j.phase -ne 'prepared') {
            throw '恢复副本缺失，无法证明当前目录归属，保留事务现场'
        }
        if (Test-Path -LiteralPath ([string]$j.stage)) {
            New-Item -ItemType Directory -Path $recovery -Force -ErrorAction Stop | Out-Null
            Move-Item -LiteralPath ([string]$j.stage) -Destination (Join-Path $recovery 'stage') -ErrorAction Stop
        }
        if ([string]$j.previous -eq 'none') {
            if ([IO.File]::Exists((Join-Path $BASE 'current.txt'))) { [IO.File]::Delete((Join-Path $BASE 'current.txt')) }
        } elseif (-not (Write-AtomicCurrent ([string]$j.previous))) { throw $script:SwitchError }
        [IO.File]::Delete($path)
        Log '已恢复中断的账号切换；候选数据保留在 recovery 目录'
        return $true
    } catch { return (Set-SwitchError "切换事务恢复失败:$($_.Exception.Message)") }
}

function Read-StunResponse([byte[]]$Response, [byte[]]$Transaction) {
    if ($Response.Length -lt 20 -or $Transaction.Length -ne 12 -or
        $Response[0] -ne 1 -or $Response[1] -ne 1) { return $null }
    $length = ([int]$Response[2] -shl 8) -bor $Response[3]
    if ($length % 4 -ne 0 -or $Response.Length -ne 20 + $length) { return $null }
    $cookie = [byte[]](0x21,0x12,0xA4,0x42)
    for ($i = 0; $i -lt 4; $i++) { if ($Response[4+$i] -ne $cookie[$i]) { return $null } }
    for ($i = 0; $i -lt 12; $i++) { if ($Response[8+$i] -ne $Transaction[$i]) { return $null } }
    $pos = 20
    $answer = $null
    while ($pos -lt $Response.Length) {
        if ($pos + 4 -gt $Response.Length) { return $null }
        $type = ([int]$Response[$pos] -shl 8) -bor $Response[$pos+1]
        $size = ([int]$Response[$pos+2] -shl 8) -bor $Response[$pos+3]
        $end = $pos + 4 + $size
        $next = $end + ((4 - ($size % 4)) % 4)
        if ($end -gt $Response.Length -or $next -gt $Response.Length) { return $null }
        if ($type -in @(1,32)) {
            if ($size -lt 4 -or $Response[$pos+4] -ne 0) { return $null }
            $family = $Response[$pos+5]
            $count = if ($family -eq 1) { 4 } elseif ($family -eq 2) { 16 } else { 0 }
            if ($count -eq 0 -or $size -ne $count + 4) { return $null }
            $address = New-Object byte[] $count
            $mask = $cookie + $Transaction
            for ($i = 0; $i -lt $count; $i++) {
                $address[$i] = $Response[$pos+8+$i]
                if ($type -eq 32) { $address[$i] = $address[$i] -bxor $mask[$i] }
            }
            $answer = (New-Object Net.IPAddress -ArgumentList (,$address)).ToString()
        }
        $pos = $next
    }
    return $answer
}

function Get-VerifiedTunStatus {
    try {
        $adapters = @(Get-NetAdapter -ErrorAction Stop | Where-Object {
            $_.Status -eq 'Up' -and ($_.InterfaceDescription -match 'TAP|TUN|WireGuard|Wintun|VPN|Tailscale|Mihomo|Clash' -or
                $_.Name -match 'TUN|WireGuard|Wintun|Mihomo|Clash')
        })
        $routes = @(Get-NetRoute -AddressFamily IPv4 -ErrorAction Stop)
        foreach ($adapter in $adapters) {
            $own = @($routes | Where-Object { $_.InterfaceIndex -eq $adapter.ifIndex -and
                $_.DestinationPrefix -in @('0.0.0.0/0','0.0.0.0/1','128.0.0.0/1') })
            if (-not $own.Count) { continue }
            $selected = @()
            foreach ($probe in @('1.1.1.1','208.67.222.222')) {
                $selected += @(Find-NetRoute -RemoteIPAddress $probe -ErrorAction Stop |
                    Where-Object { $_.PSObject.Properties.Name -contains 'DestinationPrefix' })
            }
            if ($selected.Count -ge 2 -and @($selected | Where-Object { $_.InterfaceIndex -ne $adapter.ifIndex }).Count -eq 0) {
                return @{ up = $true; detail = '活动隧道及 IPv4 路由已验证'; interface = [string]$adapter.Name }
            }
        }
        return @{ up = $false; detail = '未找到接管出站路由的活动隧道' }
    } catch { return @{ up = $false; detail = '无法验证活动接口和路由' } }
}
