"""Studio 3D à l'échelle — serveur MCP sans session, en plusieurs copies derrière le service « studio ».

Mêmes outils qu'au module s3d01 (soumettre_rendu, etat_rendu, mes_rendus) : pour Open WebUI, rien ne
change. Aucune copie ne garde d'état en mémoire : les demandes vont dans Redis, et les workers
(travailleur.py) viennent les y chercher. Chaque copie surveille aussi les demandes dont le worker a
disparu, et les remet dans la file.
"""
import asyncio
import hmac
import time

from mcp.server.fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

import commun as c

r = c.connexion()
# Sans session : chaque appel se suffit à lui-même, n'importe quelle copie peut y répondre.
mcp = FastMCP("studio", host="0.0.0.0", port=8000, stateless_http=True)


def _entetes(ctx: Context):
    requete = ctx.request_context.request
    return requete.headers if requete is not None else {}


def _utilisateur(ctx: Context) -> str:
    """Adresse de l'utilisateur (en-tête rempli par Open WebUI), après contrôle du jeton du studio."""
    entetes = _entetes(ctx)
    if entetes.get("authorization", "") != f"Bearer {c.JETON}":
        raise PermissionError("jeton du studio invalide")
    email = entetes.get("x-utilisateur-email", "").strip().lower()
    if not email:
        raise PermissionError("utilisateur inconnu : en-tête X-Utilisateur-Email absent")
    return email


def _position(numero: int) -> int | None:
    """1 = la prochaine à passer ; None si la demande n'est plus en attente."""
    en_attente = [int(x) for x in r.lrange(c.FILE, 0, -1)]  # gauche = la plus récente
    if numero not in en_attente:
        return None
    return len(en_attente) - en_attente.index(numero)


# ---------------------------------------------------------------- Outils MCP

@mcp.tool()
def soumettre_rendu(titre: str, code: str, ctx: Context) -> dict:
    """Met en file d'attente le rendu d'une scène Blender et répond immédiatement.

    code : script Python (bpy) qui construit la scène dans une scène vide : objets, matériaux,
    lumière, caméra. Ne pas régler le moteur ni enregistrer : le studio s'en charge
    (Cycles, 1280 x 720, PNG + modèle 3D GLB). Les objets de décor (sol, table, fond) doivent avoir
    un nom commençant par « Decor » : ils sont rendus mais pas exportés dans le modèle 3D.
    Renvoie le numéro de la demande et sa position dans la file."""
    utilisateur = _utilisateur(ctx)
    uid = _entetes(ctx).get("x-utilisateur-id", "").strip()  # notifications (série Intégrations)
    numero = r.incr("studio:compteur")  # atomique : numéro unique, même avec plusieurs copies
    r.hset(c.demande(numero), mapping={
        "numero": numero, "titre": titre[:80], "code": code, "utilisateur": utilisateur,
        "utilisateur_id": uid, "statut": "en_attente", "soumis": time.time(),
    })
    r.lpush(c.FILE, numero)    # la file : les workers la vident par la droite
    r.lpush(c.CHARGE, numero)  # la charge, lue par KEDA : une entrée par demande en attente ou en cours
    suite = "L'utilisateur sera prévenu dans Open WebUI." if uid else "Demandez son état avec etat_rendu."
    return {"numero": numero, "titre": titre, "position": _position(numero), "message": f"Demande enregistrée. {suite}"}


@mcp.tool()
def etat_rendu(numero: int, ctx: Context) -> dict:
    """Donne l'état d'une demande de l'utilisateur : en_attente (avec sa position), en_cours,
    termine (avec l'image en Markdown et l'artefact 3D à recopier tels quels) ou erreur."""
    utilisateur = _utilisateur(ctx)
    d = {c.texte(k): c.texte(v) for k, v in r.hgetall(c.demande(numero)).items()}
    if not d or d.get("utilisateur") != utilisateur:
        return {"numero": numero, "erreur": "aucune demande de ce numéro pour cet utilisateur"}
    etat = {"numero": numero, "titre": d["titre"], "statut": d["statut"]}
    if d["statut"] == "en_attente":
        etat["position"] = _position(numero)
    elif d["statut"] == "en_cours":
        etat["depuis_secondes"] = round(time.time() - float(d["debut"]))
        etat["travailleur"] = d.get("travailleur", "")
    elif d["statut"] == "termine":
        etat["duree_secondes"] = round(float(d["fin"]) - float(d["debut"]))
        etat["travailleur"] = d.get("travailleur", "")
        etat["image_markdown"] = f'![{d["titre"]}]({c.lien(numero, "png")})'
        etat["artefact_3d"] = c.artefact(numero, d["titre"])
    else:
        etat["erreur"] = d.get("erreur", "")
    return etat


@mcp.tool()
def mes_rendus(ctx: Context) -> list[dict]:
    """Liste les demandes de l'utilisateur connecté (statut, worker, position), de la plus récente à la plus ancienne."""
    utilisateur = _utilisateur(ctx)
    dernier = int(r.get("studio:compteur") or 0)
    resultats = []
    for n in range(dernier, 0, -1):
        d = {c.texte(k): c.texte(v) for k, v in r.hgetall(c.demande(n)).items()}
        if d.get("utilisateur") == utilisateur:
            ligne = {"numero": n, "titre": d["titre"], "statut": d["statut"]}
            if d["statut"] == "en_attente":
                ligne["position"] = _position(n)
            if d.get("travailleur"):
                ligne["travailleur"] = d["travailleur"]
            resultats.append(ligne)
    return resultats


# ---------------------------------------------------------------- Fichiers (liens signés) et santé

@mcp.custom_route("/r/{numero}/{nom}", methods=["GET"])
async def servir(request: Request) -> Response:
    try:
        numero = int(request.path_params["numero"])
        sig, extension = request.path_params["nom"].rsplit(".", 1)
    except ValueError:
        return Response(status_code=404)
    if extension not in ("png", "glb") or not hmac.compare_digest(sig, c.signature(numero, extension)):
        return Response(status_code=404)
    contenu = r.get(c.fichier(numero, extension))
    if contenu is None:
        return Response(status_code=404)
    return Response(contenu, media_type="image/png" if extension == "png" else "model/gltf-binary",
                    headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "private, max-age=3600"})


@mcp.custom_route("/sante", methods=["GET"])
async def sante(request: Request) -> Response:
    return JSONResponse({"ok": bool(r.ping()), "en_attente": r.llen(c.FILE), "en_traitement": r.llen(c.TRAITEMENT), "charge": r.llen(c.CHARGE)})


# Routes de la série Intégrations (widget de suivi, fonction Pipe) : fichier à part, absent du dépôt du module s3d02.
try:
    import integrations
    integrations.brancher(mcp, r, _position)
except ImportError:
    pass


# ---------------------------------------------------------------- Demandes orphelines

async def _surveiller() -> None:
    """Remet dans la file les demandes dont le worker ne bat plus (pod supprimé, arrêt brutal)."""
    suspects: set[int] = set()  # demandes sans battement à la ronde précédente
    while True:
        await asyncio.sleep(3)
        try:
            for brut in r.lrange(c.TRAITEMENT, 0, -1):
                n = int(brut)
                if r.exists(c.vivant(n)):  # le worker bat : tout va bien
                    suspects.discard(n)
                    continue
                if n not in suspects:      # première ronde sans battement : on attend la suivante
                    suspects.add(n)
                    continue
                suspects.discard(n)
                if r.lrem(c.TRAITEMENT, 1, brut):  # une seule copie du serveur gagne
                    r.hset(c.demande(n), mapping={"statut": "en_attente", "travailleur": ""})
                    r.rpush(c.FILE, n)  # à droite : elle repasse en premier
        except Exception as e:  # Redis indisponible un instant : on réessaie
            print(f"surveillance : {e}")


if __name__ == "__main__":
    app = mcp.streamable_http_app()
    demarrage = app.router.lifespan_context

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def cycle(application):
        tache = asyncio.create_task(_surveiller())
        async with demarrage(application):
            yield
        tache.cancel()

    app.router.lifespan_context = cycle
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
