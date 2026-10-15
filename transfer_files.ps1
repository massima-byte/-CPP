# Script de transfert des fichiers vers le serveur de production
# Remplacez la clé SSH par la clé actuelle

$sshKey = "9cab693d1253da5a1e87872f2e414dd75fe07282513e91ca0d2c4182fb5d063f"
$server = "pwsdr.mtn.bj"
$port = "4422"
$remotePath = "/home/itbreport_linux_svc/marketing-platform"
$localPath = "C:\Users\massima.BJ\OneDrive - MTN Group\Desktop\Marketing Platform"

Write-Host "Transfert des fichiers vers $server..." -ForegroundColor Green

# Créer une archive zip pour faciliter le transfert
$zipPath = "$localPath\marketing-platform.zip"
Write-Host "Création de l'archive..." -ForegroundColor Yellow
Compress-Archive -Path "$localPath\*" -DestinationPath $zipPath -Force

# Transférer l'archive via scp
Write-Host "Transfert de l'archive via scp..." -ForegroundColor Yellow
scp -P $port $zipPath "$sshKey@$server`:~/$remotePath.zip"

# Nettoyer l'archive locale
Remove-Item $zipPath

Write-Host "Transfert terminé!" -ForegroundColor Green
Write-Host "Sur le serveur, exécutez:"
Write-Host "  1. ssh -p $port $sshKey@$server"
Write-Host "  2. su - itbreport_linux_svc"
Write-Host "  3. cd ~"
Write-Host "  4. unzip ~/$remotePath.zip -d marketing-platform"
Write-Host "  5. cd marketing-platform"
Write-Host "  6. chmod +x deploy.sh"
Write-Host "  7. ./deploy.sh"
