#!/bin/bash
# Script de déploiement MTN Marketing Platform
# À exécuter après s'être connecté avec: su - itbreport_linux_svc

# Arrêter le serveur existant
echo "Arrêt du serveur existant..."
pkill -f "python -m app.server" || true

# Naviguer vers le répertoire de l'application (adapter le chemin)
cd /home/itbreport_linux_svc/marketing-platform

# Mettre à jour les dépendances si nécessaire
echo "Mise à jour des dépendances..."
python -m pip install -r requirements.txt

# Démarrer le serveur
echo "Démarrage du serveur..."
export MTN_MARKETING_PORT=8533
export MTN_MARKETING_ALLOW_REMOTE=true
export MTN_MARKETING_ALLOWED_HOSTS=10.77.60.96

nohup python -m app.server --host 0.0.0.0 --port 8533 > /var/log/marketing-monitor.log 2>&1 &

# Vérifier que le serveur est démarré
sleep 5
echo "Vérification du serveur..."
curl http://127.0.0.1:8533/api/health

echo "Déploiement terminé. Le serveur est accessible sur http://10.77.60.96:8533"
