"""Studio 3D à l'échelle — worker Blender (plusieurs copies, nombre piloté par KEDA).

Va chercher une demande dans la file quand il est libre (BLMOVE file → traitement), la rend avec un
Blender neuf (rendu.py), range les fichiers dans Redis, puis recommence. Pendant le rendu, il bat
(studio:vivant:<n>, toutes les 3 s, expire en 10 s) : s'il disparaît, le serveur remet la demande dans la file. À l'arrêt demandé
(SIGTERM, réduction du nombre de workers), il termine son rendu en cours puis s'arrête.
"""
import json
import os
import signal
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import commun as c

BLENDER = os.environ.get("BLENDER", "/opt/blender/blender")
RENDU = Path(__file__).with_name("rendu.py")
DUREE_MAX = int(os.environ.get("DUREE_MAX", "300"))
NOM = socket.gethostname()  # nom du pod
r = c.connexion()
arret = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: arret.set())


def battre(numero: int, fini: threading.Event) -> None:
    """Battement de cœur : tant que le rendu dure, une clé qui expire en 10 s, renouvelée toutes les 3 s."""
    while not fini.wait(3):
        r.setex(c.vivant(numero), 10, NOM)


def rendre(numero: int) -> None:
    d = {c.texte(k): c.texte(v) for k, v in r.hgetall(c.demande(numero)).items()}
    r.setex(c.vivant(numero), 10, NOM)
    r.hset(c.demande(numero), mapping={"statut": "en_cours", "debut": time.time(), "travailleur": NOM})
    fini = threading.Event()
    threading.Thread(target=battre, args=(numero, fini), daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dossier = Path(tmp)
            (dossier / "scene.py").write_text(d.get("code", ""), encoding="utf-8")
            # Le code du modèle s'exécute dans ce Blender, sans les variables d'environnement du worker.
            env = {"PATH": "/usr/bin:/bin", "HOME": tmp}
            sortie = subprocess.run([BLENDER, "-b", "--factory-startup", "--python", str(RENDU), "--", str(dossier)],
                                    capture_output=True, text=True, timeout=DUREE_MAX, env=env).stdout
            resultat = dossier / "resultat.json"
            if not resultat.exists():
                raise RuntimeError("\n".join(sortie.strip().splitlines()[-12:]) or "Blender n'a rien produit")
            info = json.loads(resultat.read_text(encoding="utf-8"))
            if info.get("erreur"):
                raise RuntimeError(info["erreur"])
            r.setex(c.fichier(numero, "png"), c.DUREE_FICHIERS, (dossier / "rendu.png").read_bytes())
            r.setex(c.fichier(numero, "glb"), c.DUREE_FICHIERS, (dossier / "modele.glb").read_bytes())
        r.hset(c.demande(numero), mapping={"statut": "termine", "fin": time.time()})
    except subprocess.TimeoutExpired:
        r.hset(c.demande(numero), mapping={"statut": "erreur", "fin": time.time(), "erreur": f"rendu interrompu après {DUREE_MAX} s"})
    except Exception as e:  # le message revient au modèle, qui peut corriger et resoumettre
        r.hset(c.demande(numero), mapping={"statut": "erreur", "fin": time.time(), "erreur": str(e)[-1500:]})
    finally:
        fini.set()
        r.lrem(c.TRAITEMENT, 1, numero)
        r.lrem(c.CHARGE, 1, numero)  # demande finie (réussie ou en erreur) : KEDA peut réduire
        r.delete(c.vivant(numero))
        if d.get("utilisateur_id"):  # notifications (série Intégrations) : le notificateur préviendra l'utilisateur
            r.lpush(c.NOTIFICATIONS, numero)


def main() -> None:
    print(f"{NOM} prêt", flush=True)
    while not arret.is_set():  # arrêt demandé (SIGTERM) : on finit le rendu en cours, puis on sort
        # Attend une demande 2 s au plus, et la déplace d'un seul geste de la file vers « en cours ».
        brut = r.blmove(c.FILE, c.TRAITEMENT, 2, "RIGHT", "LEFT")
        if brut is None:
            continue
        numero = int(brut)
        print(f"{NOM} : demande {numero}", flush=True)
        rendre(numero)
    print(f"{NOM} arrêté", flush=True)


if __name__ == "__main__":
    main()
