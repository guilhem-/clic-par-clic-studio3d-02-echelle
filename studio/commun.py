"""Studio 3D à l'échelle : ce que partagent le serveur MCP (api.py) et les workers (travailleur.py).

Tout l'état vit dans Redis, pour que le serveur tourne en plusieurs copies sans session et que
les workers aillent eux-mêmes chercher les demandes :

  studio:compteur              numéro de la prochaine demande (INCR)
  studio:demande:<n>           hash : titre, code, utilisateur, utilisateur_id, statut, soumis, debut, fin, erreur, travailleur
  studio:file                  demandes en attente (LPUSH à gauche, prises à droite)
  studio:traitement            demandes prises par un worker (BLMOVE file → traitement)
  studio:charge                une entrée par demande en attente ou en cours : la mesure de KEDA
  studio:vivant:<n>            battement de cœur du worker qui traite <n> (expire en 10 s)
  studio:fichier:<n>:<ext>     PNG et GLB produits (expirent en 24 h ; en production : stockage objet)
  studio:notifications         rendus finis à annoncer (série Intégrations, lus par le notificateur)
"""
import hashlib
import hmac
import os

import redis

JETON = os.environ.get("STUDIO_JETON", "")
SECRET = os.environ.get("STUDIO_SECRET", JETON).encode()
PUBLIC = os.environ.get("STUDIO_PUBLIC_URL", "http://studio.localhost:8080").rstrip("/")
DUREE_FICHIERS = int(os.environ.get("DUREE_FICHIERS", str(24 * 3600)))

FILE, TRAITEMENT, CHARGE, NOTIFICATIONS = "studio:file", "studio:traitement", "studio:charge", "studio:notifications"
demande = lambda n: f"studio:demande:{n}"
vivant = lambda n: f"studio:vivant:{n}"
fichier = lambda n, ext: f"studio:fichier:{n}:{ext}"


def connexion() -> redis.Redis:
    return redis.Redis(host=os.environ.get("REDIS_HOTE", "redis"), port=6379,
                       password=os.environ.get("REDIS_MOT_DE_PASSE") or None, decode_responses=False)


def texte(valeur) -> str:
    return valeur.decode() if isinstance(valeur, bytes) else (valeur or "")


def signature(numero: int, extension: str) -> str:
    return hmac.new(SECRET, f"{numero}.{extension}".encode(), hashlib.sha256).hexdigest()[:10]


def lien(numero: int, extension: str) -> str:
    return f"{PUBLIC}/r/{numero}/{signature(numero, extension)}.{extension}"


def artefact(numero: int, titre: str) -> str:
    return (
        '```html\n'
        '<script type="module" src="https://cdn.jsdelivr.net/npm/@google/model-viewer@4.0.0/dist/model-viewer.min.js"></script>\n'
        f'<model-viewer src="{lien(numero, "glb")}" poster="{lien(numero, "png")}" alt="{titre}" '
        'camera-controls auto-rotate shadow-intensity="1" '
        'style="width:100%;height:480px;background:#f4f4f5"></model-viewer>\n'
        '```'
    )
