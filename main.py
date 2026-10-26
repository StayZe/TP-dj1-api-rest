import json

import requests
from fastapi import FastAPI, HTTPException

app = FastAPI()


@app.get("/")
async def root():
    return {"message": "Hello World"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/containers")
async def containers():
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
async def container(id: str):
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
