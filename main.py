import json
import os
import secrets
from typing import Optional

import requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse

load_dotenv()

app = FastAPI()

# Nombre de remédiations faites depuis le démarrage de l'API
remediations = 0

# Codes d'erreur réutilisés dans la documentation
ERR_401 = {401: {"description": "Token absent ou inconnu"}}
ERR_403 = {403: {"description": "Token valide mais rôle lecteur"}}


def get_role(authorization: Optional[str] = Header(default=None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")

    token = authorization[len("Bearer "):]
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if secrets.compare_digest(token, os.environ.get("API_TOKEN_ADMIN", "")):
        return "admin"
    if secrets.compare_digest(token, os.environ.get("API_TOKEN_READER", "")):
        return "reader"

    raise HTTPException(status_code=401, detail="Unauthorized")


def require_admin(role: str = Depends(get_role)) -> str:
    if role != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    return role


def netbox_headers():
    return {
        "Authorization": "Token " + os.environ["NETBOX_TOKEN"],
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def netbox_url(path):
    return os.environ["NETBOX_URL"].rstrip("/") + "/api/virtualization/" + path


def netbox_find_vm(name):
    response = requests.get(
        netbox_url("virtual-machines/"),
        headers=netbox_headers(),
        params={"name": name},
    )
    results = response.json()["results"]
    return results[0] if results else None


def netbox_create_vm(name):
    cluster_response = requests.get(
        netbox_url("clusters/"),
        headers=netbox_headers(),
        params={"name": "docker-local"},
    )
    cluster = cluster_response.json()["results"][0]
    requests.post(
        netbox_url("virtual-machines/"),
        headers=netbox_headers(),
        json={"name": name, "cluster": cluster["id"], "status": "active"},
    )


def netbox_set_status(name, status):
    vm = netbox_find_vm(name)
    if vm:
        requests.patch(
            netbox_url(f"virtual-machines/{vm['id']}/"),
            headers=netbox_headers(),
            json={"status": status},
        )


def netbox_delete_vm(name):
    vm = netbox_find_vm(name)
    if vm:
        requests.delete(
            netbox_url(f"virtual-machines/{vm['id']}/"),
            headers=netbox_headers(),
        )


@app.get("/", summary="Message de bienvenue")
async def root():
    return {"message": "Hello World"}


@app.get("/health", summary="Vérifie que l'API répond")
async def health():
    return {"status": "ok"}


@app.get(
    "/containers",
    summary="Liste les conteneurs gérés par l'API",
    responses={**ERR_401},
)
async def containers(role: str = Depends(get_role)):
    filters = json.dumps({"label": ["adminapi.managed=true"]})
    response = requests.get(
        "http://127.0.0.1:2375/containers/json",
        params={"all": 1, "filters": filters},
    )
    return [
        {
            "id": c["Id"],
            "name": c["Names"][0].lstrip("/"),
            "image": c["Image"],
            "state": c["State"],
        }
        for c in response.json()
    ]


@app.get(
    "/containers/{id}",
    summary="Donne le détail d'un conteneur géré par l'API",
    responses={**ERR_401, 404: {"description": "Conteneur inconnu ou non géré"}},
)
async def container(id: str, role: str = Depends(get_role)):
    response = requests.get(f"http://127.0.0.1:2375/containers/{id}/json")
    if response.status_code != 200:
        raise HTTPException(status_code=404, detail="Not found")

    data = response.json()
    labels = data["Config"]["Labels"] or {}
    if labels.get("adminapi.managed") != "true":
        raise HTTPException(status_code=404, detail="Not found")

    return {
        "id": data["Id"],
        "name": data["Name"].lstrip("/"),
        "image": data["Config"]["Image"],
        "state": data["State"]["Status"],
    }


@app.post(
    "/containers",
    status_code=201,
    summary="Crée et démarre un conteneur géré, avec sa machine virtuelle NetBox",
    responses={
        **ERR_401,
        **ERR_403,
        409: {"description": "Le nom est déjà pris"},
        422: {"description": "Image absente, hors allowlist, ou nom invalide"},
    },
)
async def create_container(request: dict, role: str = Depends(require_admin)):
    name = request.get("name", "")
    image = request.get("image")

    if not image:
        raise HTTPException(status_code=422, detail="image is required")

    if image not in ["nginx:alpine", "alpine:latest"]:
        raise HTTPException(status_code=422, detail="image not allowed")

    if name == "" or name != name.lower() or " " in name:
        raise HTTPException(status_code=422, detail="invalid name")

    body = {"Image": image, "Labels": {"adminapi.managed": "true"}}
    response = requests.post(
        "http://127.0.0.1:2375/containers/create",
        params={"name": name},
        json=body,
    )
    if response.status_code == 409:
        raise HTTPException(status_code=409, detail="name already taken")
    if response.status_code != 201:
        raise HTTPException(status_code=response.status_code, detail=response.json().get("message"))

    container_id = response.json()["Id"]
    requests.post(f"http://127.0.0.1:2375/containers/{container_id}/start")

    netbox_create_vm(name)

    info = requests.get(f"http://127.0.0.1:2375/containers/{container_id}/json").json()
    return {
        "id": container_id,
        "name": name,
        "image": image,
        "state": info["State"]["Status"],
    }


@app.post(
    "/containers/{id}/stop",
    summary="Arrête un conteneur géré et passe sa machine virtuelle en offline",
    responses={
        **ERR_401,
        **ERR_403,
        404: {"description": "Conteneur inconnu"},
        409: {"description": "Conteneur déjà arrêté"},
    },
)
async def stop_container(id: str, role: str = Depends(require_admin)):
    response = requests.post(f"http://127.0.0.1:2375/containers/{id}/stop")
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Not found")
    if response.status_code == 304:
        raise HTTPException(status_code=409, detail="already stopped")

    info = requests.get(f"http://127.0.0.1:2375/containers/{id}/json").json()
    netbox_set_status(info["Name"].lstrip("/"), "offline")
    return {"id": id, "state": info["State"]["Status"]}


@app.post(
    "/containers/{id}/start",
    summary="Démarre un conteneur géré et repasse sa machine virtuelle en active",
    responses={
        **ERR_401,
        **ERR_403,
        404: {"description": "Conteneur inconnu"},
        409: {"description": "Conteneur déjà démarré"},
    },
)
async def start_container(id: str, role: str = Depends(require_admin)):
    response = requests.post(f"http://127.0.0.1:2375/containers/{id}/start")
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Not found")
    if response.status_code == 304:
        raise HTTPException(status_code=409, detail="already started")

    info = requests.get(f"http://127.0.0.1:2375/containers/{id}/json").json()
    netbox_set_status(info["Name"].lstrip("/"), "active")
    return {"id": id, "state": info["State"]["Status"]}


@app.delete(
    "/containers/{id}",
    status_code=204,
    summary="Supprime un conteneur géré et sa machine virtuelle NetBox",
    responses={
        **ERR_401,
        **ERR_403,
        404: {"description": "Conteneur inconnu"},
    },
)
async def delete_container(id: str, role: str = Depends(require_admin)):
    info_response = requests.get(f"http://127.0.0.1:2375/containers/{id}/json")
    if info_response.status_code == 404:
        raise HTTPException(status_code=404, detail="Not found")
    name = info_response.json()["Name"].lstrip("/")

    requests.delete(f"http://127.0.0.1:2375/containers/{id}", params={"force": 1})
    netbox_delete_vm(name)
    return None


@app.post(
    "/webhooks/alerts",
    status_code=202,
    summary="Reçoit une alerte container_down et redémarre le conteneur visé",
    responses={
        401: {"description": "Secret X-Webhook-Secret absent ou faux"},
        404: {"description": "Conteneur inconnu"},
        422: {"description": "Type d'alerte inconnu"},
    },
)
async def alert(request: dict, x_webhook_secret: Optional[str] = Header(default=None)):
    global remediations

    expected = os.environ.get("WEBHOOK_SECRET", "")
    if not x_webhook_secret or not secrets.compare_digest(x_webhook_secret, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")

    if request.get("alert") != "container_down":
        raise HTTPException(status_code=422, detail="unknown alert type")

    name = request.get("container", "")
    check = requests.get(f"http://127.0.0.1:2375/containers/{name}/json")
    if check.status_code == 404:
        raise HTTPException(status_code=404, detail="Not found")

    # 204 : le conteneur a été redémarré, c'est une remédiation.
    # 304 : il tournait déjà, on ne compte rien.
    start = requests.post(f"http://127.0.0.1:2375/containers/{name}/start")
    if start.status_code == 204:
        remediations += 1

    return {"status": "accepted"}


@app.get(
    "/metrics",
    response_class=PlainTextResponse,
    summary="Métriques au format Prometheus (sans token)",
)
async def metrics():
    filters = json.dumps({"label": ["adminapi.managed=true"]})
    response = requests.get(
        "http://127.0.0.1:2375/containers/json",
        params={"all": "true", "filters": filters},
    )

    compte = {}
    for c in response.json():
        etat = c["State"]
        compte[etat] = compte.get(etat, 0) + 1

    lignes = ["# TYPE adminapi_containers gauge"]
    for etat, n in compte.items():
        lignes.append(f'adminapi_containers{{state="{etat}"}} {n}')

    lignes.append("# TYPE adminapi_remediations_total counter")
    lignes.append(f"adminapi_remediations_total {remediations}")

    return "\n".join(lignes) + "\n"
