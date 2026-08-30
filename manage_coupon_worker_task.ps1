<#
.SYNOPSIS
    Instala/gerencia a Scheduled Task do Coupon Collector no Windows.

.DESCRIPTION
    Worker autocontido (sem dependência do repositório AIShoppingAgent).
    Precisa de sessão Windows LOGADA (auto-logon) -- o mesmo requisito já
    documentado para o collection_worker principal
    (docs/architecture/windows-collection-worker.md do AIShoppingAgent):
    o Edge dedicado (coupons/edge_transport.py) precisa de uma estação de
    janela real; tela BLOQUEADA é suportada, sessão DESLOGADA não.

    Por isso o Principal da tarefa usa -LogonType Interactive amarrado ao
    usuário informado (nunca ServiceAccount/S4U/Password).

    Achado já validado no projeto principal: o reinício automático nativo
    do Task Scheduler NÃO dispara para um processo morto externamente
    (Stop-Process -Force) -- só ajuda quando o próprio processo termina
    sozinho com código de erro. Este script configura o restart nativo
    mesmo assim (ajuda no caso de crash real), mas não substitui um
    supervisor externo -- este worker não tem um (proporcional ao seu
    risco/frequência, bem menor que o collection_worker principal).

.PARAMETER Action
    Install | Update | Status | Enable | Disable | Remove

.PARAMETER TaskUser
    Usuário (DOMAIN\User ou .\User) dono da sessão interativa/auto-logon.
    Obrigatório em Install/Update.

.PARAMETER PythonPath
    Caminho do python.exe do venv deste projeto. Default: .venv\Scripts\python.exe
    dentro desta mesma pasta.

.PARAMETER WhatIf
    Mostra o que seria feito, sem registrar/alterar a tarefa.

.EXAMPLE
    powershell -File manage_coupon_worker_task.ps1 -Action Install -TaskUser "CESAR-SERVER\Administrator"
    powershell -File manage_coupon_worker_task.ps1 -Action Status
    powershell -File manage_coupon_worker_task.ps1 -Action Remove
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Install", "Update", "Status", "Enable", "Disable", "Remove")]
    [string]$Action,

    [string]$TaskUser,

    [string]$PythonPath,

    [string]$TaskName = "AIShoppingCoupon-Worker"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not $PythonPath) {
    $PythonPath = Join-Path $ScriptDir ".venv\Scripts\python.exe"
}
$WorkerScript = Join-Path $ScriptDir "worker.py"

function Assert-InteractiveRequirements {
    if (-not (Test-Path $PythonPath)) {
        throw "Python do venv nao encontrado em '$PythonPath'. Rode install.ps1 primeiro (cria o venv e instala requirements.txt)."
    }
    if (-not (Test-Path $WorkerScript)) {
        throw "worker.py nao encontrado em '$ScriptDir'."
    }
}

switch ($Action) {
    "Status" {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $task) {
            Write-Host "Tarefa '$TaskName' nao existe."
            return
        }
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host "Tarefa: $TaskName"
        Write-Host "  Estado:            $($task.State)"
        Write-Host "  Ultima execucao:   $($info.LastRunTime)"
        Write-Host "  Ultimo resultado:  0x$('{0:X8}' -f $info.LastTaskResult)"
        Write-Host "  Proxima execucao:  $($info.NextRunTime)"
        return
    }
    "Remove" {
        if ($PSCmdlet.ShouldProcess($TaskName, "Remove-ScheduledTask")) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
            Write-Host "Tarefa '$TaskName' removida (se existia)."
        }
        return
    }
    "Enable" {
        if ($PSCmdlet.ShouldProcess($TaskName, "Enable-ScheduledTask")) {
            Enable-ScheduledTask -TaskName $TaskName | Out-Null
            Write-Host "Tarefa '$TaskName' habilitada."
        }
        return
    }
    "Disable" {
        if ($PSCmdlet.ShouldProcess($TaskName, "Disable-ScheduledTask")) {
            Disable-ScheduledTask -TaskName $TaskName | Out-Null
            Write-Host "Tarefa '$TaskName' desabilitada."
        }
        return
    }
    { $_ -in "Install", "Update" } {
        Assert-InteractiveRequirements
        if (-not $TaskUser) {
            throw "-TaskUser e obrigatorio em Install/Update (ex.: 'CESAR-SERVER\Administrator'), precisa ser o usuario da sessao com auto-logon."
        }

        $action = New-ScheduledTaskAction `
            -Execute $PythonPath `
            -Argument "`"$WorkerScript`"" `
            -WorkingDirectory $ScriptDir

        # Dispara no logon do usuario -- precisa de sessao interativa real
        # (Edge dedicado nao funciona em Session 0 / Windows Service).
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $TaskUser

        $principal = New-ScheduledTaskPrincipal `
            -UserId $TaskUser `
            -LogonType Interactive `
            -RunLevel Limited

        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -StartWhenAvailable `
            -RestartCount 3 `
            -RestartInterval (New-TimeSpan -Minutes 2) `
            -ExecutionTimeLimit (New-TimeSpan -Hours 0) # sem limite -- eh um daemon de loop continuo

        if ($PSCmdlet.ShouldProcess($TaskName, "Register-ScheduledTask ($Action)")) {
            Register-ScheduledTask `
                -TaskName $TaskName `
                -Action $action `
                -Trigger $trigger `
                -Principal $principal `
                -Settings $settings `
                -Description "Coupon Collector Worker (AIShoppingAgent TASK-106) -- projeto separado, Edge/CDP dedicado." `
                -Force | Out-Null
            Write-Host "Tarefa '$TaskName' registrada para o usuario '$TaskUser'."
            Write-Host "Inicie manualmente agora com: Start-ScheduledTask -TaskName '$TaskName'"
        }
        return
    }
}
