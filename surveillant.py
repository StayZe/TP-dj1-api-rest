#!/usr/bin/env python3
"""Surveillant : joue le rôle de l'outil de supervision qui émet les alertes.

Toutes les 5 secondes, il demande à Docker la liste des conteneurs gérés. Pour chaque
conteneur arrêté, il envoie une alerte « container_down » au webhook de votre API.

    python surveillant.py

Il lit le fichier .env du dossier courant (WEBHOOK_SECRET, DOCKER_API_URL).
Pour le voir agir : arrêtez un conteneur géré avec « docker stop <nom> ».
"""

import json
import os
import time

import requests

API = "http://127.0.0.1:8000"
PERIODE = 5  # secondes


def lire_env(chemin=".env"):
    valeurs = {}
    if os.path.exists(chemin):
        with open(chemin, encoding="utf-8") as fichier:
            for ligne in fichier:
                ligne = ligne.strip()
                if ligne and not ligne.startswith("#") and "=" in ligne:
                    cle, _, valeur = ligne.partition("=")
                    valeurs[cle.strip()] = valeur.strip()
    valeurs.update(os.environ)
    return valeurs


def conteneurs_arretes(docker_url):
    filtres = json.dumps({"label": ["adminapi.managed=true"], "status": ["exited"]})
    reponse = requests.get(
        f"{docker_url}/containers/json", params={"all": "true", "filters": filtres}, timeout=10
    )
    reponse.raise_for_status()
    return [conteneur["Names"][0].lstrip("/") for conteneur in reponse.json()]


def alerter(nom, secret):
    reponse = requests.post(
        f"{API}/webhooks/alerts",
        json={"alert": "container_down", "container": nom},
        headers={"X-Webhook-Secret": secret},
        timeout=30,
    )
    print(f"alerte container_down pour {nom} -> {reponse.status_code} {reponse.text[:80]}", flush=True)


def main():
    env = lire_env()
    docker_url = env.get("DOCKER_API_URL", "http://127.0.0.1:2375")
    secret = env.get("WEBHOOK_SECRET", "")
    print(f"Surveillance de {docker_url}, alertes vers {API}. Ctrl+C pour arrêter.", flush=True)
    while True:
        try:
            for nom in conteneurs_arretes(docker_url):
                alerter(nom, secret)
        except requests.RequestException as erreur:
            print(f"service injoignable : {type(erreur).__name__}", flush=True)
        time.sleep(PERIODE)


if __name__ == "__main__":
    main()
