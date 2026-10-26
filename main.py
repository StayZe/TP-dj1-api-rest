import json
import os
import secrets
from typing import Optional

import requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException

load_dotenv()

app = FastAPI()


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


@app.get("/")
async def root():
    return {"message": "Hello World"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/containers")
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


@app.get("/containers/{id}")
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


@app.post("/containers", status_code=201)
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

    info = requests.get(f"http://127.0.0.1:2375/containers/{container_id}/json").json()
    return {
        "id": container_id,
        "name": name,
        "image": image,
        "state": info["State"]["Status"],
    }


@app.post("/containers/{id}/stop")
async def stop_container(id: str, role: str = Depends(require_admin)):
    response = requests.post(f"http://127.0.0.1:2375/containers/{id}/stop")
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Not found")
    if response.status_code == 304:
        raise HTTPException(status_code=409, detail="already stopped")

    info = requests.get(f"http://127.0.0.1:2375/containers/{id}/json").json()
    return {"id": id, "state": info["State"]["Status"]}


@app.post("/containers/{id}/start")
async def start_container(id: str, role: str = Depends(require_admin)):
    response = requests.post(f"http://127.0.0.1:2375/containers/{id}/start")
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Not found")
    if response.status_code == 304:
        raise HTTPException(status_code=409, detail="already started")

    info = requests.get(f"http://127.0.0.1:2375/containers/{id}/json").json()
    return {"id": id, "state": info["State"]["Status"]}


@app.delete("/containers/{id}", status_code=204)
async def delete_container(id: str, role: str = Depends(require_admin)):
    response = requests.delete(f"http://127.0.0.1:2375/containers/{id}", params={"force": 1})
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Not found")
    return None
