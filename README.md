# MTN Enterprise Intelligence — Marketing Monitoring

Tableau de bord local de suivi marketing MTN Bénin. L’application extrait des
agrégats récents depuis Presto, les conserve dans un snapshot local et les
présente avec la date de partition de chaque source.

## Démarrage

1. Installer Python 3.11 ou supérieur, puis les dépendances :

   ```powershell
   python -m pip install -r requirements.txt
   ```

2. Configurer Presto et LDAP dans `.env` à partir de `.env.example`.
   En l’absence de `.env` à la racine du projet, la configuration locale
   `churn/.env` est réutilisée sans copier ni afficher ses secrets. Pour la
   validation TLS, définir `MTN_PRESTO_TLS_VERIFY` avec le chemin du certificat
   CA MTN approuvé. La valeur `false` est conservée pour compatibilité avec le
   connecteur déjà utilisé dans cet environnement; elle chiffre la connexion
   mais ne valide pas le certificat du serveur.

3. Lancer le serveur local :

   ```powershell
   .\scripts\run_dashboard.ps1
   ```

4. Ouvrir <http://127.0.0.1:8532> et se connecter avec le compte professionnel
  LDAP. Le serveur vérifie Presto en arrière-plan
   toutes les 15 minutes, même si aucun navigateur n’est ouvert. Au démarrage,
   il lance une extraction si le snapshot manque ou a dépassé cet intervalle.
   Le bouton ↻ permet également une actualisation manuelle. La page recharge
   le snapshot publié et vérifie l’état du serveur chaque minute.

Le serveur écoute uniquement sur loopback. Toutes les routes `/api/*` de données
exigent une session LDAP; le cookie de session est HttpOnly, SameSite=Strict et
expire après `MTN_MARKETING_SESSION_TIMEOUT` secondes (3600 par défaut). Les
sessions sont gardées en mémoire et sont invalidées au redémarrage. Le mode
recommandé est LDAPS sur 636 avec validation obligatoire du certificat; fournir
la CA interne via `MTN_LDAP_CA_CERT_FILE` ou l’installer dans le magasin de
confiance système. Pour reproduire la configuration DQ Platform, définir
`MTN_LDAP_USE_TLS=false` et utiliser le port 389. **Ce mode envoie le mot de
passe LDAP sans chiffrement entre le serveur applicatif et LDAP**; il est activé
ici explicitement pour compatibilité avec l’environnement MTN existant.

## Comptes, profils et audit

LDAP authentifie d’abord chaque utilisateur. Un identifiant LDAP valide mais
inconnu de la plateforme ne reçoit pas de session : une demande d’accès est
créée et l’interface indique qu’elle attend une décision Admin. Les connexions
suivantes gardent une seule demande ouverte. Après approbation, le prochain
login LDAP crée une session HttpOnly. Un compte suspendu reste authentifié par
LDAP mais reçoit un refus d’accès applicatif.

La base locale `data/marketing_access.sqlite3` est créée automatiquement et
exclue du contrôle de version. Son emplacement peut être remplacé par
`MTN_MARKETING_ACCESS_DB`. Elle ne stocke aucun mot de passe LDAP. Le compte
`massima` est créé ou restauré comme Administrateur actif au démarrage; le
backend interdit de changer son profil ou de le suspendre. Il se connecte avec
son compte LDAP habituel, sans mot de passe local ajouté.

Profils initiaux :

- `Viewer` : consulter le tableau de bord.
- `Analyste` : consulter, exporter et actualiser les données Presto.
- `Administrateur` : toutes les permissions; ses droits sont fixes.

Les privilèges sont séparés : `view_dashboard`, `export_data`, `refresh_data`,
`review_account_requests`, `manage_accounts`, `manage_profiles` et
`view_audit_logs`. Le traitement des demandes, la gestion des comptes et
l’édition de la matrice des profils peuvent donc être confiés à des personnes
différentes. L’Analyste reçoit export et actualisation; le Viewer n’a que la
consultation. Les Administrateurs ont tous les droits. Les permissions sont
évaluées à chaque requête; le bouton Export est aussi masqué et vérifié dans
l’interface.

Selon leurs privilèges, les administrateurs approuvent ou refusent les demandes,
choisissent un profil, modifient les noms/emails, suspendent les comptes ou
ajustent les privilèges Viewer et Analyste. Les panneaux Admin et les droits
sont vérifiés sur chaque route serveur; masquer un onglet dans l’interface ne
constitue pas un contrôle d’accès.
Les changements s’appliquent à la prochaine requête et les sessions des comptes
suspendus sont invalidées à leur prochaine requête.

Tables SQLite : `users` (identité et profil), `profiles`, `privileges`,
`profile_privileges` (autorisation et affectation), `account_requests`
(historique des décisions) et `audit_logs`. L’audit journalise les connexions,
demandes, décisions, mises à jour de comptes/profils, déconnexions, refus
d’autorisation et actualisations de données. Il conserve acteur, cible, heure,
résultat et détails non secrets; les mots de passe ne sont jamais journalisés.
L’onglet Administration est réservé aux administrateurs et présente les
demandes, comptes, matrices de privilèges et 100 derniers événements.

Par défaut, le port d’écoute reste loopback. Pour reproduire le déploiement DQ
Platform sur un réseau MTN de confiance, définir explicitement
`MTN_MARKETING_ALLOW_REMOTE=true`, `MTN_MARKETING_ALLOWED_HOSTS=10.77.60.96`
et démarrer avec `--host 0.0.0.0`. Les API conservent l’authentification LDAP,
et les POST vérifient l’origine same-origin. Le trafic navigateur reste HTTP :
le mot de passe de connexion et le cookie de session ne sont pas chiffrés sur
le réseau. N’activer ce mode que sur le réseau interne approuvé; pour un accès
partagé hors de ce périmètre, configurer un reverse proxy HTTPS.

Sans bind distant, depuis un poste client, utiliser un tunnel SSH local
(adapter le compte géré selon le serveur) :

```powershell
ssh -N -L 18533:127.0.0.1:8533 -p 4422 <compte-géré>@pwsdr.mtn.bj
```

Puis ouvrir <http://127.0.0.1:18533>. Pour un accès partagé, configurer un
reverse proxy HTTPS avec authentification et origines autorisées avant
l’exposition réseau.

Les routes de données sont `/api/health`, `/api/metrics` et `POST /api/refresh`.
Les snapshots sont
enregistrés sous `data/marketing_metrics.json`, exclus du contrôle de version.
Le port d’écoute par défaut est `8532`; il peut être remplacé avec la variable
d’environnement `MTN_MARKETING_PORT`. Le serveur reste lié à `127.0.0.1` et ne
doit pas être exécuté en tant que `root`. Pour un hébergement public, prévoir
un reverse proxy TLS et une configuration d’accès dédiée avant le déploiement.
La cadence Presto se règle avec `MTN_MARKETING_REFRESH_INTERVAL_SECONDS`
(900 secondes par défaut, minimum 60). Une extraction échouée est signalée dans
l’interface et dans les journaux du serveur; le dernier snapshot valide reste
disponible. La fraîcheur réelle dépend aussi de la disponibilité et des dates
de partition publiées par Presto.
Si une requête échoue, le message du module et du dernier essai identifie
également la table source concernée lorsque Presto a fourni ce détail.

## Rafraîchissement manuel et tests

```powershell
python .\scripts\refresh_data.py
python -m unittest discover -s tests -v
```

## Périmètre et fraîcheur

- Les indicateurs exécutifs et la tendance des revenus utilisent
  `itbi_ess.prod_kpis_revenu`; SIM, inscriptions, Mobile Money, services et
  revenu géographique sont extraits de leurs propres partitions récentes.
- Le tableau exécutif compare le revenu de la dernière journée disponible à
  celui de la veille calendaire seulement si cette partition est présente. La
  variation en FCFA reste disponible si le revenu de référence vaut zéro; le
  pourcentage est alors laissé vide plutôt que calculé artificiellement.
- Real-time BI agrège `feeds.air_refill_ma` par date et heure de l’événement
  sur les trois dernières partitions de chargement. Il affiche une fenêtre de
  24 heures, met en évidence la dernière heure écoulée complète et compare
  cette heure à la précédente si les deux créneaux consécutifs sont observés;
  l’heure courante partielle est exclue.
- Les dates affichées sont celles des partitions effectivement retournées.
  Les sommes d’inscription représentent des lignes source, pas des personnes
  uniques. Les activations Air Refill ne sont pas assimilées à des clients.
- La géographie clientèle compte les MSISDN distincts ayant une activité de
  recharge par département dans la dernière partition de
  `itbi_ess.datamart_refill_loc_vw`. Ce n’est ni le stock national d’abonnés
  actifs ni un décompte garanti de personnes uniques; la somme des départements
  peut différer du total national distinct. La partition du jour est signalée
  comme partielle.
- Les indicateurs support couvrent les 30 jours complets précédant la date
  d’extraction. Les tickets et les conversations YelloChat, ainsi que leurs
  distincts source respectifs, sont publiés séparément; les tickets sont aussi
  ventilés par statut source, avec contrôle que les catégories totalisent le
  nombre de tickets. Des séries quotidiennes séparées sont affichées et
  exportées pour chaque source; leurs distincts ne sont pas additionnés ou
  dédupliqués entre systèmes.
- Mobile Money expose également les volumes, frais, transactions et MSISDN
  distincts par type d’opération de la source. Les distincts de catégories
  peuvent se chevaucher et ne doivent pas être additionnés; le champ `partner`
  n’est pas assimilé à un commerçant actif. Une tendance de sept jours présente
  les montants journaliers, les transactions, les frais et les distincts à la
  date de chaque partition.
- Le RGS90 n’est pas publié : le calcul de distincts sur 90 jours depuis
  `feeds.air_refill_ma` dépasse la limite Presto de 90 secondes et la
  définition métier/source reste à valider. Le catalogue de fraîcheur le
  signale comme indisponible; aucune valeur de remplacement n’est utilisée.
- Le module Services inclut une tendance de sept jours du montant et du nombre
  d’opérations d’Air Refill au-dessus du seuil source de 100 FCFA. Les jours
  sans partition restent vides; la partition du jour est signalée comme
  partielle et cette série n’est pas un décompte de clients.
- La partition d’inscriptions IRM la plus récente peut être en retard sur la
  date courante; ce retard est exposé dans le catalogue de fraîcheur plutôt
  que présenté comme une donnée à jour.
- L’onglet Geographic superpose les agrégats Presto aux limites administratives
  des 12 départements du Bénin, embarquées localement dans
  `static/benin_departments.geojson`. Les limites proviennent de geoBoundaries
  et Wikimedia Commons (année représentée : 2012; domaine public); elles ne
  constituent pas une source de métriques marketing.
- Aucun enregistrement individuel, numéro de téléphone, Customer 360,
  segmentation client ou analyse/prédiction de churn n’est extrait ou exporté.
- Le snapshot existant est préservé si une actualisation échoue; l’interface
  affiche alors explicitement l’erreur plutôt que de présenter des valeurs
  nouvelles.
- Une somme source absente n’est pas remplacée par zéro; l’actualisation est
  refusée et le dernier snapshot valide est conservé. Les moyennes dont le
  dénominateur est nul restent « non disponible ».
