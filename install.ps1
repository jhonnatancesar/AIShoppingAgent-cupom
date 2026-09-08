<#
.SYNOPSIS
    Prepara o Coupon Collector no Windows: venv, dependencias e .env.

.DESCRIPTION
    Nao instala nenhum navegador -- usa o Microsoft Edge ja instalado no
    Windows (coupons/edge_transport.py descobre o caminho automaticamente
    ou usa "edge_executable" em config.json se voce apontar outro).

    Depois deste script, registre a Scheduled Task com:
        powershell -File manage_coupon_worker_task.ps1 -Action Install -TaskUser "DOMINIO\Usuario"

.EXAMPLE
    powershell -File install.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

function Log($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "`nERRO: $msg" -ForegroundColor Red; exit 1 }

Log "1/4 verificando Microsoft Edge instalado"
$edgeCandidates = @(
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "$env:LocalAppData\Microsoft\Edge\Application\msedge.exe"
)
$edgeFound = $edgeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $edgeFound) {
    Fail "Microsoft Edge nao encontrado nos caminhos padrao. Instale o Edge ou defina `"edge_executable`" em config.json."
}
Log "Edge encontrado em: $edgeFound"

Log "2/4 venv + pacotes Python"
if (-not (Test-Path ".venv")) {
    python -m venv .venv
}
$venvPython = Join-Path $ScriptDir ".venv\Scripts\python.exe"
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r requirements.txt

Log "3/4 token de controle (.env)"
if (-not (Test-Path ".env")) {
    $bytes = New-Object byte[] 36
    # `RandomNumberGenerator::Fill` (static) só existe em .NET moderno --
    # Windows PowerShell 5.1 (a base do Windows Server) roda .NET
    # Framework, onde só `::Create().GetBytes(bytes)` (instância) existe.
    # Achado real de deploy: `::Fill` levanta MethodNotFound nesse host,
    # silenciosamente deixando $bytes zerado (o erro não interrompe o
    # script por padrão) -- token fraco/previsível gerado sem aviso.
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $token = [Convert]::ToBase64String($bytes) -replace '[+/=]', ''
    # `-Encoding utf8` do Windows PowerShell 5.1 grava BOM (achado real:
    # quebrou `cesar_core_api_key` do GG Oferta do mesmo jeito) -- `ascii`
    # nunca grava BOM e o token é só base64 (sem acento), sem perda.
    "# Gerado pelo install.ps1. Troque se quiser.`nAUTH_TOKEN=$token`n" | Set-Content -Encoding ascii ".env"
    Remove-Variable token, bytes
    Log "Novo .env criado com token gerado."
} else {
    Log "Arquivo .env ja existe; mantendo (nao sobrescrito)."
}

Log "4/4 validacao de descoberta de cadencia"
& $venvPython -c @"
from cadence import now_sp, mode_for, next_slot, parse_promo_window
from coupons.persistence import PromoWindow
w = parse_promo_window(PromoWindow())
now = now_sp()
mode = mode_for(now, w)
print(f'agora (America/Sao_Paulo): {now.isoformat()}')
print(f'modo atual: {mode}')
for _ in range(3):
    slot = next_slot(now, mode)
    print(f'  proximo slot: {slot.isoformat()}')
    now = slot
"@

Log "Instalacao concluida."
Write-Host "Proximo passo -- registrar a Scheduled Task:"
Write-Host "  powershell -File manage_coupon_worker_task.ps1 -Action Install -TaskUser `"DOMINIO\Usuario`""
Write-Host "`nTestar sem Scheduled Task (rodada unica):"
Write-Host "  .venv\Scripts\python.exe worker.py --once"
