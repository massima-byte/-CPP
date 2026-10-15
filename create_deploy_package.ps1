# Script pour créer le package de déploiement
$localPath = "C:\Users\massima.BJ\OneDrive - MTN Group\Desktop\Marketing Platform"
$zipPath = "$localPath\marketing-platform-deploy.zip"

Write-Host "Création du package de déploiement..." -ForegroundColor Green

# Créer une archive zip
Compress-Archive -Path "$localPath\*" -DestinationPath $zipPath -Force

Write-Host "Package créé: $zipPath" -ForegroundColor Green
Write-Host ""
Write-Host "Instructions de déploiement:" -ForegroundColor Yellow
Write-Host "1. Transférer marketing-platform-deploy.zip vers le serveur via votre méthode préférée"
Write-Host "2. Sur le serveur:"
Write-Host "   ssh -p 4422 9cab693d1253da5a1e87872f2e414dd75fe07282513e91ca0d2c4182fb5d063f@pwsdr.mtn.bj"
Write-Host "   su - itbreport_linux_svc"
Write-Host "   cd ~"
Write-Host "   unzip marketing-platform-deploy.zip -d marketing-platform"
Write-Host "   cd marketing-platform"
Write-Host "   chmod +x deploy.sh"
Write-Host "   ./deploy.sh"
