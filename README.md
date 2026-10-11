# Passer le studio 3D à l'échelle — Redis, workers Blender et KEDA

Code de la démonstration du module 2 de la série « Clic par clic » **Créer un service d'IA
compatible agentique : le studio 3D**.

Vidéo : {{video}}

## But

Le studio du module 1 tenait sur un seul pod : un Blender, une file en mémoire, perdue au moindre
redémarrage. Ce dépôt le fait passer à l'échelle **sans que rien ne change pour Open WebUI** (même
service `studio`, mêmes outils MCP) :

- le **serveur MCP** tourne en deux copies **sans session** (`stateless_http=True`) : n'importe
  laquelle répond, aucune ne garde d'état en mémoire ;
- tout l'état vit dans **Redis** : compteur, une table par demande, file d'attente, liste des
  demandes en cours, charge ;
- les **workers Blender** vont eux-mêmes chercher le travail quand ils sont libres (`BLMOVE`
  file → en cours, atomique) : pas de répartiteur réseau, un rendu à la fois par worker ;
- un **battement de cœur** (clé Redis renouvelée toutes les 3 s, expirant en 10 s) : si un worker
  disparaît en plein rendu, le serveur remet sa demande en tête de file ;
- **KEDA** règle le nombre de workers selon la liste `studio:charge` (une entrée par demande en
  attente ou en cours) : de zéro à quatre, retour à zéro quand tout est fini.

## Contenu

| Fichier | Rôle |
|---|---|
| `studio/commun.py` | Ce que partagent serveur et workers : clés Redis, connexion, liens signés |
| `studio/api.py` | Serveur MCP sans session : outils `soumettre_rendu`, `etat_rendu`, `mes_rendus`, rendus signés `/r/…`, santé `/sante`, surveillance des demandes orphelines |
| `studio/travailleur.py` | Worker : boucle `BLMOVE`, rendu par un Blender neuf sans interface, battement de cœur, arrêt propre sur `SIGTERM` |
| `studio/rendu.py` | Exécuté par Blender : construit la scène, ajoute caméra et lumière au besoin, rend l'image, exporte le GLB (inchangé depuis le module 1) |
| `studio/Dockerfile` | Une image pour les deux rôles : Python 3.12, Blender 5.2 (miroir officiel, empreinte SHA-256 vérifiée), client Redis |
| `k8s-echelle.yaml` | Redis, serveur MCP ×2, service `studio`, workers (0 copie au départ), `ScaledObject` KEDA, publication des rendus, règles réseau |
| `open-webui/` | En-tête d'identité de la connexion MCP et prompt système du modèle « Studio 3D » |
| `essai/demandes.sh` | Envoie quatre demandes au nom de deux utilisateurs, comme Open WebUI le ferait |
| `.env.example` | Variables à renseigner (jeton partagé, mot de passe Redis), sans valeur |

## Prérequis

- Le studio du **module 1** et sa connexion dans Open WebUI (dépôt
  [`clic-par-clic-studio3d-01-studio-mcp`](https://github.com/guilhem-/clic-par-clic-studio3d-01-studio-mcp)) :
  ce module en part. On peut aussi partir de zéro en suivant les étapes 4 et 5 du README du module 1
  pour la connexion et le modèle.
- Docker, `kubectl` et un cluster Kubernetes. La vidéo utilise **k3d** (cluster `tterm`, Traefik
  publié sur le port 8080 de la machine).
- **KEDA 2.21** (installé à l'étape 2).
- Environ 2 processeurs et 2 Go de mémoire par worker (quatre workers au plus).

## Installation pas à pas

1. **Image** (serveur et workers partagent la même) :

   ```bash
   docker build -t studio3d:2 studio
   k3d image import studio3d:2 -c tterm      # k3d ; sinon, poussez l'image dans votre registre
   ```

2. **KEDA**, une fois par cluster :

   ```bash
   kubectl apply --server-side -f https://github.com/kedacore/keda/releases/download/v2.21.0/keda-2.21.0.yaml
   kubectl -n keda rollout status deployment/keda-operator
   ```

3. **Secrets.** Copiez `.env.example` en `.env`, renseignez `STUDIO_JETON` (celui du module 1) et
   `REDIS_MOT_DE_PASSE` (par exemple `openssl rand -hex 24`), puis :

   ```bash
   set -a; . ./.env; set +a
   kubectl create namespace studio3d --dry-run=client -o yaml | kubectl apply -f -
   kubectl -n studio3d create secret generic studio \
     --from-literal=jeton=$STUDIO_JETON --from-literal=redis=$REDIS_MOT_DE_PASSE \
     --dry-run=client -o yaml | kubectl apply -f -
   ```

4. **Du studio simple au studio à l'échelle.** Retirez le déploiement et la règle réseau du
   module 1 (le service `studio` est repris tel quel), puis appliquez le manifeste :

   ```bash
   kubectl -n studio3d delete deployment/studio networkpolicy/studio-sans-sortie --ignore-not-found
   kubectl apply -f k8s-echelle.yaml
   kubectl -n studio3d rollout status deployment/studio-api
   ```

   Adaptez dans `k8s-echelle.yaml` l'adresse publique des rendus (`STUDIO_PUBLIC_URL`, hôte de
   l'`Ingress`) à votre cluster.

5. **Open WebUI** : rien à changer si la connexion « Studio 3D » du module 1 existe (même URL
   `http://studio.studio3d.svc.cluster.local:8000/mcp`). Pour la démonstration de la vidéo, le
   prompt système du modèle est `open-webui/consignes-modele.txt` (il ajoute la liste des rendus
   avec le worker de chacun).

6. **Essai.** Dans un terminal, suivez les workers :

   ```bash
   kubectl -n studio3d get pods -l app=studio-travailleur -w
   ```

   Dans un autre, ouvrez un accès local et envoyez des demandes :

   ```bash
   kubectl -n studio3d port-forward svc/studio 8000:8000 &
   ./essai/demandes.sh
   ```

   En même temps, demandez deux rendus depuis Open WebUI. KEDA démarre les workers en quelques
   secondes. Pour simuler une panne, supprimez un worker en plein rendu :

   ```bash
   kubectl -n studio3d delete pod <nom-du-worker> --grace-period=0 --force
   ```

   Une quinzaine de secondes plus tard, sa demande repasse « en attente » puis est reprise par un
   autre worker (`curl http://127.0.0.1:8000/sante`). Une trentaine de secondes après le dernier
   rendu, les workers s'arrêtent jusqu'à zéro.

## Points à connaître

- **KEDA et plusieurs déclencheurs** : KEDA retient le plus grand, pas la somme. D'où une seule
  liste, `studio:charge`, qui compte les demandes en attente **et** en cours.
- **Arrêt d'un worker** : `terminationGracePeriodSeconds: 120` ; sur `SIGTERM`, le worker finit
  son rendu avant de s'arrêter. Une suppression forcée (`--force`) simule une vraie panne.
- **Règles réseau** : le serveur et les workers ne joignent que Redis (et la résolution de noms) ;
  Redis n'accepte que le studio et KEDA. Elles ne s'appliquent que si le réseau du cluster gère les
  `NetworkPolicy` (c'est le cas de k3d).
- **`utilisateur_id` et `studio:notifications`** : prévus pour la série suivante (notification de
  l'utilisateur dans Open WebUI) ; sans en-tête `X-Utilisateur-Id`, ils restent inutilisés.

## Sécurité et production

- Le point MCP (`/mcp`) n'est pas publié hors du cluster ; seuls les rendus (`/r/…`) le sont, par
  des liens signés (HMAC).
- Limites de cet exemple : Redis garde aussi les fichiers (24 h) ; le code écrit par le modèle
  s'exécute dans le worker, qui peut joindre Redis. En production : stockage objet pour les
  fichiers, Blender isolé dans un conteneur sans réseau, priorités par groupe d'utilisateurs,
  cartes graphiques pour les rendus, secrets dans votre coffre.

## Suite de la série

- Module précédent : **Un studio 3D pour toute l'organisation** — dépôt
  [`clic-par-clic-studio3d-01-studio-mcp`](https://github.com/guilhem-/clic-par-clic-studio3d-01-studio-mcp).
- Module suivant : **Observer le studio avec Grafana** (mesures Prometheus, tableau de bord et
  alerte provisionnés par fichiers) — dépôt
  [`clic-par-clic-studio3d-03-grafana`](https://github.com/guilhem-/clic-par-clic-studio3d-03-grafana).

## Licence

MIT, voir [`LICENSE`](LICENSE). Blender est distribué sous licence GPL par la Blender Foundation ;
Redis et KEDA sont des projets indépendants. Cette série est indépendante des éditeurs des
logiciels présentés.
