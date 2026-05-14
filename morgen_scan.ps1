param([double]$Konto = 10000, [double]$Risiko = 1.0)

$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("Path","User")

$SYMBOLS  = @("BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","LINKUSDT","NEARUSDT","AVAXUSDT","MAGICUSDT","DOTUSDT","ADAUSDT","XRPUSDT","SUIUSDT","INJUSDT","APTUSDT","ARBUSDT")
$RISK_USD = $Konto * $Risiko / 100
$L1 = "=" * 64
$L2 = "-" * 64

function Get-EMA($arr, $period) {
    $k = 2 / ($period + 1); $e = $arr[0]
    for ($i = 1; $i -lt $arr.Count; $i++) { $e = $arr[$i] * $k + $e * (1 - $k) }
    return $e
}
function Round-Price($v) {
    if ($v -gt 100) { return [math]::Round($v, 2) }
    if ($v -gt 1)   { return [math]::Round($v, 4) }
    return [math]::Round($v, 5)
}

# ── KILLZONE ERKENNUNG (UTC-Zeit) ─────────────────────────────────────────────
# London Killzone : 07:00 - 10:00 UTC  (09:00 - 12:00 CEST)
# NY Killzone     : 12:00 - 15:00 UTC  (14:00 - 17:00 CEST)
$utcHour    = (Get-Date).ToUniversalTime().Hour
$utcMinute  = (Get-Date).ToUniversalTime().Minute
$utcDecimal = $utcHour + $utcMinute / 60

$isLondonKZ = ($utcDecimal -ge 7.0  -and $utcDecimal -lt 10.0)
$isNYKZ     = ($utcDecimal -ge 12.0 -and $utcDecimal -lt 15.0)
$isKillzone = $isLondonKZ -or $isNYKZ

if ($isLondonKZ)    { $kzName = "LONDON KILLZONE (07:00-10:00 UTC)" }
elseif ($isNYKZ)    { $kzName = "NY KILLZONE (12:00-15:00 UTC)" }
else                { $kzName = "" }

Clear-Host
Write-Host $L1 -ForegroundColor Cyan
Write-Host ("  MORGEN-SCAN  |  {0}  |  `${1}  |  Risiko {2}% (`${3})" -f (Get-Date -Format "yyyy-MM-dd HH:mm"), $Konto, $Risiko, $RISK_USD) -ForegroundColor Cyan
if ($isKillzone) {
    Write-Host ("  MODUS: ALERT  -- {0}" -f $kzName) -ForegroundColor Magenta
    Write-Host ("  Keine Market Orders -- Alarm bei Entry-Preis setzen!") -ForegroundColor Magenta
} else {
    Write-Host ("  MODUS: ENTRY  -- Normale Handelszeit") -ForegroundColor Green
}
Write-Host $L1 -ForegroundColor Cyan

# ── BLOCK 1: MAKRO-KALENDER ───────────────────────────────────────────────────
Write-Host ""
Write-Host "  [1/3] MAKRO-KALENDER" -ForegroundColor Yellow
Write-Host $L2 -ForegroundColor DarkGray

$skipToday = $false
try {
    $cal   = Invoke-RestMethod "https://nfs.faireconomy.media/ff_calendar_thisweek.json" -TimeoutSec 6
    $today = (Get-Date).ToString("yyyy-MM-dd")
    $high  = $cal | Where-Object { $_.impact -eq "High" -and $_.country -eq "USD" -and $_.date -like "$today*" }

    if ($high) {
        $skipToday = $true
        Write-Host "  WARNUNG: Heute High-Impact USD News!" -ForegroundColor Red
        foreach ($ev in $high) {
            $t = try { ([datetime]$ev.date).ToString("HH:mm") } catch { "?" }
            Write-Host ("  {0}  {1}" -f $t, $ev.title) -ForegroundColor Red
        }
        Write-Host "  >> Kein Trade heute empfohlen." -ForegroundColor Red
    } else {
        Write-Host "  Heute: Keine High-Impact News. Grünes Licht." -ForegroundColor Green
        $next = $cal | Where-Object { $_.impact -eq "High" -and $_.country -eq "USD" } | Select-Object -First 2
        foreach ($ev in $next) {
            $d = try { ([datetime]$ev.date).ToString("MM-dd HH:mm") } catch { $ev.date }
            Write-Host ("  Naechste: {0}  {1}" -f $d, $ev.title) -ForegroundColor DarkGray
        }
    }
} catch {
    Write-Host "  Kalender offline -- prüfen: forexfactory.com/calendar" -ForegroundColor DarkGray
}

# ── BLOCK 2: FUNDING RATE ─────────────────────────────────────────────────────
Write-Host ""
Write-Host "  [2/3] FUNDING RATE" -ForegroundColor Yellow
Write-Host $L2 -ForegroundColor DarkGray

$fundingWarn = $false
try {
    $fr      = Invoke-RestMethod "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT" -TimeoutSec 6
    $fPct    = [math]::Round([double]$fr.lastFundingRate * 100, 4)
    $fAnnual = [math]::Round($fPct * 3 * 365, 1)

    if ($fPct -gt 0.05) {
        $fundingWarn = $true
        Write-Host ("  BTC Funding: +{0}%  -- HOCH! Zu viele Longs, Squeeze-Risiko." -f $fPct) -ForegroundColor Red
    } elseif ($fPct -lt -0.01) {
        Write-Host ("  BTC Funding: {0}%  -- NEGATIV. Gut fuer Longs." -f $fPct) -ForegroundColor Green
    } else {
        Write-Host ("  BTC Funding: {0}%  -- Neutral. OK." -f $fPct) -ForegroundColor Green
    }
    Write-Host ("  Annualisiert: {0}% p.a." -f $fAnnual) -ForegroundColor DarkGray

    Write-Host "  Funding alle Coins:" -ForegroundColor DarkGray
    foreach ($sym in $SYMBOLS) {
        try {
            $f2   = Invoke-RestMethod "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=$sym" -TimeoutSec 4
            $fp   = [math]::Round([double]$f2.lastFundingRate * 100, 4)
            $coin = $sym -replace "USDT", ""
            $fcol = if ($fp -gt 0.05) { "Red" } elseif ($fp -lt -0.01) { "Green" } else { "White" }
            Write-Host ("    {0,-6} {1,7}%" -f $coin, $fp) -ForegroundColor $fcol
        } catch {}
    }
} catch {
    Write-Host "  Funding Rate offline -- prüfen: coinglass.com/funding" -ForegroundColor DarkGray
}

# ── BLOCK 3: ETF FLOWS ────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  [3/3] BTC ETF FLOWS" -ForegroundColor Yellow
Write-Host $L2 -ForegroundColor DarkGray

try {
    $etf = Invoke-RestMethod "https://open-api.coinglass.com/public/v2/etf" -TimeoutSec 6
    if ($etf.data) {
        $flow = [double]($etf.data | Select-Object -First 1).totalNetAssets
        Write-Host ("  ETF Net Assets: `${0}B" -f [math]::Round($flow / 1e9, 2)) -ForegroundColor White
    } else {
        Write-Host "  Keine ETF-Daten -- SoSoValue: sosovalue.com/assets/etf/us-bitcoin-spot" -ForegroundColor DarkGray
    }
} catch {
    Write-Host "  ETF Flows -- manuell prüfen:" -ForegroundColor DarkGray
    Write-Host "    sosovalue.com/assets/etf/us-bitcoin-spot" -ForegroundColor DarkGray
    Write-Host "    coinglass.com/bitcoin-etf" -ForegroundColor DarkGray
    Write-Host "  Grün = Zufluss (Long-Rückenwind)  |  Rot = Abfluss (vorsichtig)" -ForegroundColor DarkGray
}

# ── FILTER-ZUSAMMENFASSUNG ────────────────────────────────────────────────────
Write-Host ""
Write-Host $L1 -ForegroundColor Cyan

if ($skipToday) {
    Write-Host "  FILTER: NEWS-TAG -- Kein Trade heute!" -ForegroundColor Red
    Write-Host $L1 -ForegroundColor Cyan
    exit
}
if ($fundingWarn) {
    Write-Host "  FILTER: Funding hoch -- Risiko halbiert auf 0.5% (`${0})" -f ($RISK_USD / 2) -ForegroundColor Yellow
    $RISK_USD = $RISK_USD / 2
}
Write-Host ("  Filter OK -- scanne {0} Coins auf 4h..." -f $SYMBOLS.Count) -ForegroundColor Green
Write-Host $L1 -ForegroundColor Cyan

# ── BLOCK 4: EMA20 SCAN ───────────────────────────────────────────────────────
Write-Host ""
Write-Host "  EMA20 PULLBACK SCAN -- 4h + Daily" -ForegroundColor Cyan
Write-Host $L2 -ForegroundColor DarkGray

$setups  = @()
$watch   = @()
$noSetup = @()

foreach ($sym in $SYMBOLS) {
    $coin = $sym -replace "USDT", ""
    try {
        $r4 = Invoke-RestMethod "https://api.binance.com/api/v3/klines?symbol=$sym&interval=4h&limit=50" -TimeoutSec 8
        $rd = Invoke-RestMethod "https://api.binance.com/api/v3/klines?symbol=$sym&interval=1d&limit=25" -TimeoutSec 8

        $c4     = $r4 | ForEach-Object { [double]$_[4] }
        $o4     = $r4 | ForEach-Object { [double]$_[1] }
        $h4     = $r4 | ForEach-Object { [double]$_[2] }
        $l4     = $r4 | ForEach-Object { [double]$_[3] }
        $v4     = $r4 | ForEach-Object { [double]$_[5] }
        $cd     = $rd | ForEach-Object { [double]$_[4] }

        $ema4h      = Get-EMA $c4 20
        $ema4h_prev = Get-EMA $c4[0..($c4.Count - 4)] 20
        $emaDay     = Get-EMA $cd 20
        $volAvg     = ($v4 | Measure-Object -Average).Average

        $cur = $c4[-1]; $opn = $o4[-1]; $hi = $h4[-1]; $lo = $l4[-1]; $vol = $v4[-1]
        $pl  = $l4[-2]; $ph  = $h4[-2]; $pc = $c4[-2]; $po = $o4[-2]

        $trend4hUp  = $ema4h -gt $ema4h_prev
        $trendDayUp = $cd[-1] -gt $emaDay
        $bothUp     = $trend4hUp -and $trendDayUp

        $zone       = $ema4h * 0.005
        $inZone     = ($lo -le $ema4h + $zone) -and ($hi -ge $ema4h - $zone)
        $prevInZone = ($pl -le $ema4h + $zone) -and ($ph -ge $ema4h - $zone)
        $bounceNow  = $inZone     -and ($cur -gt $ema4h) -and ($cur -gt $opn)
        $bouncePrev = $prevInZone -and ($pc  -gt $ema4h) -and ($pc  -gt $po) -and ($cur -gt $ema4h)
        $volOk      = $vol -ge $volAvg * 0.7
        $distPct    = [math]::Round(($cur - $ema4h) / $ema4h * 100, 2)

        if ($bothUp -and ($bounceNow -or $bouncePrev)) {
            $entry   = Round-Price $cur
            $sl_low  = Round-Price ($pl * 0.999)
            $sl_ema  = Round-Price ($ema4h * 0.997)
            $sl      = Round-Price ([math]::Min($sl_low, $sl_ema))
            $riskPt  = $entry - $sl
            $slPct   = [math]::Round($riskPt / $entry * 100, 2)

            if ($slPct -gt 1.5) {
                $noSetup += [PSCustomObject]@{ Coin=$coin; Grund="SL $slPct% > 1.5%"; AbstEMA="$distPct%" }
                continue
            }

            $tp      = Round-Price ($entry + $riskPt * 2.0)
            $tpPct   = [math]::Round($riskPt * 2 / $entry * 100, 2)
            $posSize = [math]::Round($RISK_USD / $riskPt, 4)
            $posVal  = [math]::Round($posSize * $entry, 2)
            $volStr  = if ($volOk) { "OK" } else { "schwach" }

            $setups += [PSCustomObject]@{
                Coin=$coin; Entry=$entry; SL=$sl; TP=$tp
                SLpct=$slPct; TPpct=$tpPct
                PosSize=$posSize; PosVal=$posVal; VolOK=$volStr
                EMA4h=(Round-Price $ema4h)
            }
        } elseif ($bothUp -and $inZone) {
            $watch += [PSCustomObject]@{
                Coin=$coin; AbstEMA="$distPct%"; Preis=(Round-Price $cur); EMA4h=(Round-Price $ema4h)
            }
        } else {
            $r = if (-not $trendDayUp) { "Daily bearish" } elseif (-not $trend4hUp) { "4h bearish" } else { "kein Pullback" }
            $noSetup += [PSCustomObject]@{ Coin=$coin; Grund=$r; AbstEMA="$distPct%" }
        }
    } catch {
        $noSetup += [PSCustomObject]@{ Coin=$coin; Grund="API Fehler"; AbstEMA="-" }
    }
}

# ── AUSGABE ───────────────────────────────────────────────────────────────────
if ($setups.Count -gt 0) {
    Write-Host ""
    if ($isKillzone) {
        Write-Host ("  ALERT-SETUPS -- {0}" -f $kzName) -ForegroundColor Magenta
        Write-Host "  Kein sofortiger Einstieg -- Alarm bei Entry setzen!" -ForegroundColor Magenta
    } else {
        Write-Host "  AKTIVE SETUPS -- JETZT HANDELBAR" -ForegroundColor Green
    }
    Write-Host $L2 -ForegroundColor $(if ($isKillzone) { "Magenta" } else { "Green" })

    foreach ($s in $setups) {
        Write-Host ""
        if ($isKillzone) {
            Write-Host ("  {0} -- ALARM SETZEN (nicht jetzt einsteigen!)" -f $s.Coin) -ForegroundColor Magenta
        } else {
            Write-Host ("  {0} -- EMA20 Pullback Long" -f $s.Coin) -ForegroundColor Green
        }
        Write-Host ("  Entry       : {0}" -f $s.Entry)
        Write-Host ("  Stop Loss   : {0}  (-{1}% unter Entry)" -f $s.SL, $s.SLpct)
        Write-Host ("  Take Profit : {0}  (+{1}% ueber Entry)  [2:1]" -f $s.TP, $s.TPpct)
        Write-Host ("  Pos.Groesse : {0} {1}  =  `${2}" -f $s.PosSize, $s.Coin, $s.PosVal)
        Write-Host ("  Risiko      : `${0}  >>  Gewinn bei TP: `${1}" -f $RISK_USD, [math]::Round($RISK_USD * 2, 2))
        Write-Host ("  Volumen     : {0}   |   EMA20 4h: {1}" -f $s.VolOK, $s.EMA4h)

        if ($isKillzone) {
            Write-Host ""
            Write-Host "  >> SO ALARM SETZEN IN TRADINGVIEW:" -ForegroundColor Yellow
            Write-Host ("     1. Chart oeffnen (Link unten)") -ForegroundColor Yellow
            Write-Host ("     2. Rechtsklick auf Preis {0} >> 'Add Alert at this price'" -f $s.Entry) -ForegroundColor Yellow
            Write-Host ("     3. Condition: '{0}/USDT  Crossing  {1}'" -f $s.Coin, $s.Entry) -ForegroundColor Yellow
            Write-Host ("     4. Notification: App + Email aktivieren") -ForegroundColor Yellow
            Write-Host ("     5. Warten bis Alarm ausloest -- DANN erst einsteigen") -ForegroundColor Yellow
            Write-Host ("     6. Beim Einstieg: SL bei {0}, TP bei {1}" -f $s.SL, $s.TP) -ForegroundColor Yellow

            # TradingView Chart oeffnen
            $tvUrl = "https://www.tradingview.com/chart/?symbol=BINANCE:{0}USDT&interval=240" -f $s.Coin
            Start-Process $tvUrl
            Write-Host ("     TradingView Chart geoeffnet: {0}" -f $tvUrl) -ForegroundColor DarkGray
        } else {
            $chartArgs = @("C:\Users\Gianni\tradingbot\chart_server.py", ($s.Coin + "USDT"), "4h", $s.Entry, $s.SL, $s.TP, $Konto, $Risiko)
            Start-Process python -ArgumentList $chartArgs -WindowStyle Hidden
            Write-Host "  Chart mit Levels wird geoeffnet..." -ForegroundColor DarkGray
        }
        Start-Sleep -Seconds 2
    }
} else {
    Write-Host ""
    Write-Host "  Keine aktiven Setups gerade." -ForegroundColor DarkGray
}

if ($watch.Count -gt 0) {
    Write-Host ""
    Write-Host "  BEOBACHTEN -- Naechste 4h Kerze abwarten" -ForegroundColor Yellow
    Write-Host $L2 -ForegroundColor Yellow
    $watch | Format-Table Coin, AbstEMA, Preis, EMA4h -AutoSize
}

Write-Host ""
Write-Host "  KEIN SETUP" -ForegroundColor DarkGray
Write-Host $L2 -ForegroundColor DarkGray
$noSetup | Format-Table Coin, Grund, AbstEMA -AutoSize

Write-Host $L1 -ForegroundColor Cyan
Write-Host "  Naechster Scan: ~15:30 (NY Open)" -ForegroundColor DarkGray
Write-Host $L1 -ForegroundColor Cyan
Write-Host ""
