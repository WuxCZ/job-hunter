# Spust jednou: pravym tlacitkem -> Spustit v PowerShell
# Vytvori "Spustit JobHunter.lnk" se ikonou (bezne Run / aplikace ve Windows)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$bat  = Join-Path $here "Start.bat"
$lnk  = Join-Path $here "Spustit JobHunter.lnk"

$Wsh = New-Object -ComObject WScript.Shell
$s = $Wsh.CreateShortcut($lnk)
$s.TargetPath = $bat
$s.WorkingDirectory = $here
$s.Description = "JobHunter Pro – spusteni GUI"
# Vychozi: 259 = ikona Spustit (šípka) – nejbližší k „běžící“ akci ve Windows
# Alternativa: 44 = silueta osoby. Zmen cislo za carkou v IconLocation.
$s.IconLocation = "$env:SystemRoot\System32\shell32.dll,259"
$s.WindowStyle = 1
$s.Save()

Write-Host "Hotovo: $lnk"
Write-Host "Dvojklik na Spustit JobHunter.lnk (muzes si ho presunout na plochu)."
