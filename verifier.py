#!/usr/bin/env python3
"""Vérificateur du projet « micro-API d'administration ».

Joue les requêtes du contrat contre votre API et dit, bloc par bloc, ce qui passe.

    python verifier.py                  tous les blocs
    python verifier.py lecture          un seul bloc
    python verifier.py lecture ecriture plusieurs blocs
    python verifier.py --api http://127.0.0.1:8000

Blocs : lecture, ecriture, authentification, netbox, webhook, metriques.

La configuration est lue dans le fichier .env du dossier courant (les variables
d'environnement déjà définies l'emportent). Le vérificateur parle à trois services :
votre API, l'API Docker (pour constater ce qui s'est réellement passé) et NetBox.

Il ne crée et ne supprime que des conteneurs dont le nom commence par « verif- ».
Seule dépendance : requests.
"""

import argparse
import json
import os
import random
import string
import sys
import time

import requests

PREFIXE = "verif-"
ETIQUETTE = "adminapi.managed"
CLUSTER = "docker-local"
IMAGE = "nginx:alpine"
DELAI = 20


class Echec(AssertionError):
    """Une attente du contrat n'est pas respectée."""


def attendu(reponse, *codes):
    """Vérifie le code de statut d'une réponse."""
    if reponse.status_code not in codes:
        voulu = " ou ".join(str(c) for c in codes)
        raise Echec(f"code {voulu} attendu, reçu {reponse.status_code} : {reponse.text[:120]!r}")
    return reponse


def lire_env(chemin=".env"):
    """Lit un fichier .env (CLE=valeur), sans écraser l'environnement existant."""
    valeurs = {}
    if os.path.exists(chemin):
        with open(chemin, encoding="utf-8") as fichier:
            for ligne in fichier:
                ligne = ligne.strip()
                if ligne and not ligne.startswith("#") and "=" in ligne:
                    cle, _, valeur = ligne.partition("=")
                    valeurs[cle.strip()] = valeur.strip().strip("\"'")
    valeurs.update(os.environ)
    return valeurs


class Verificateur:
    def __init__(self, api, env):
        self.api_url = api.rstrip("/")
        self.docker_url = env.get("VERIF_DOCKER_API_URL", "http://127.0.0.1:2375").rstrip("/")
        self.netbox_url = env.get("VERIF_NETBOX_URL", "http://127.0.0.1:8080").rstrip("/")
        self.netbox_token = env.get("NETBOX_TOKEN", "")
        self.jetons = {
            "admin": env.get("API_TOKEN_ADMIN", ""),
            "lecteur": env.get("API_TOKEN_READER", ""),
            "inconnu": "jeton-que-personne-n-a-emis",
        }
        self.secret_webhook = env.get("WEBHOOK_SECRET", "")
        self.resultats = {}   # bloc -> [(libellé, réussi, détail)]
        self.bloc = None
        self.suffixe = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))

    # ---- accès aux trois services -------------------------------------------------

    def api(self, methode, chemin, jeton="admin", **options):
        """Appelle votre API. jeton : "admin", "lecteur", "inconnu" ou None (aucun)."""
        entetes = options.pop("headers", {})
        if jeton and self.jetons.get(jeton):
            entetes.setdefault("Authorization", f"Bearer {self.jetons[jeton]}")
        try:
            return requests.request(
                methode, self.api_url + chemin, headers=entetes, timeout=DELAI, **options
            )
        except requests.ConnectionError:
            raise Echec(f"API injoignable sur {self.api_url}")
        except requests.Timeout:
            raise Echec(f"aucune réponse de l'API en {DELAI} s")

    def docker(self, methode, chemin, **options):
        return requests.request(methode, self.docker_url + chemin, timeout=60, **options)

    def netbox(self, methode, chemin, **options):
        entetes = {"Authorization": f"Bearer {self.netbox_token}", "Accept": "application/json"}
        return requests.request(
            methode, f"{self.netbox_url}/api{chemin}", headers=entetes, timeout=DELAI, **options
        )

    # ---- constats faits directement sur Docker et NetBox ---------------------------

    def nom(self, role):
        return f"{PREFIXE}{role}-{self.suffixe}"

    def etat_docker(self, identifiant):
        """État réel d'un conteneur d'après Docker, ou None s'il n'existe pas."""
        reponse = self.docker("GET", f"/containers/{identifiant}/json")
        if reponse.status_code == 404:
            return None
        detail = reponse.json()
        return {
            "id": detail["Id"],
            "state": detail["State"]["Status"],
            "labels": detail["Config"]["Labels"] or {},
            "image": detail["Config"]["Image"],
        }

    def lancer_directement(self, nom, gere=True):
        """Crée un conteneur sans passer par votre API (comme le ferait un collègue)."""
        corps = {"Image": IMAGE, "Labels": {ETIQUETTE: "true"} if gere else {}}
        reponse = self.docker("POST", "/containers/create", params={"name": nom}, json=corps)
        if reponse.status_code == 404:
            raise Echec(f"image {IMAGE} absente : lancez « docker pull {IMAGE} »")
        reponse.raise_for_status()
        identifiant = reponse.json()["Id"]
        self.docker("POST", f"/containers/{identifiant}/start").raise_for_status()
        return identifiant

    def attendre_etat(self, identifiant, etat, secondes=10):
        fin = time.time() + secondes
        while time.time() < fin:
            constat = self.etat_docker(identifiant)
            if constat and constat["state"] == etat:
                return True
            time.sleep(0.5)
        return False

    def machine_netbox(self, nom):
        reponse = self.netbox(
            "GET", "/virtualization/virtual-machines/", params={"name": nom, "cluster": CLUSTER}
        )
        reponse.raise_for_status()
        resultats = reponse.json()["results"]
        return resultats[0] if resultats else None

    def preparer_netbox(self):
        """Crée le cluster docker-local s'il manque (type de cluster « Docker »)."""
        reponse = self.netbox("GET", "/virtualization/clusters/", params={"name": CLUSTER})
        reponse.raise_for_status()
        if reponse.json()["results"]:
            return
        types = self.netbox("GET", "/virtualization/cluster-types/", params={"slug": "docker"})
        types.raise_for_status()
        if types.json()["results"]:
            id_type = types.json()["results"][0]["id"]
        else:
            creation = self.netbox(
                "POST", "/virtualization/cluster-types/", json={"name": "Docker", "slug": "docker"}
            )
            creation.raise_for_status()
            id_type = creation.json()["id"]
        self.netbox(
            "POST", "/virtualization/clusters/", json={"name": CLUSTER, "type": id_type}
        ).raise_for_status()

    def nettoyer(self):
        """Supprime tout ce que le vérificateur a pu laisser derrière lui."""
        try:
            filtres = json.dumps({"name": [PREFIXE]})
            for conteneur in self.docker(
                "GET", "/containers/json", params={"all": "true", "filters": filtres}
            ).json():
                self.docker("DELETE", f"/containers/{conteneur['Id']}", params={"force": "true"})
        except requests.RequestException:
            pass
        if self.netbox_token:
            try:
                machines = self.netbox(
                    "GET", "/virtualization/virtual-machines/", params={"name__isw": PREFIXE}
                )
                for machine in machines.json().get("results", []):
                    self.netbox("DELETE", f"/virtualization/virtual-machines/{machine['id']}/")
            except (requests.RequestException, ValueError):
                pass

    # ---- enregistrement des constats ----------------------------------------------

    def test(self, libelle):
        """Décorateur : exécute tout de suite la fonction et note si elle passe."""

        def executer(fonction):
            try:
                fonction()
                constat = (libelle, True, "")
            except Echec as echec:
                constat = (libelle, False, str(echec))
            except AssertionError as echec:
                constat = (libelle, False, str(echec) or "attente non respectée")
            except requests.RequestException as erreur:
                constat = (libelle, False, f"service injoignable : {type(erreur).__name__}")
            except Exception as erreur:  # réponse mal formée, clé absente, etc.
                constat = (libelle, False, f"{type(erreur).__name__} : {erreur}")
            self.resultats.setdefault(self.bloc, []).append(constat)
            return fonction

        return executer


# ==== Les blocs ====================================================================


def bloc_lecture(v):
    """TP3 : /health, liste, détail, 404."""
    nom_gere, nom_etranger = v.nom("lecture"), v.nom("etranger")
    ids = {}

    @v.test('GET /health répond 200 et {"status": "ok"}')
    def _():
        reponse = attendu(v.api("GET", "/health", jeton=None), 200)
        assert reponse.json() == {"status": "ok"}, f"corps reçu : {reponse.text[:80]}"

    @v.test("GET /containers répond 200 avec une liste JSON")
    def _():
        ids["gere"] = v.lancer_directement(nom_gere)
        ids["etranger"] = v.lancer_directement(nom_etranger, gere=False)
        reponse = attendu(v.api("GET", "/containers", jeton="lecteur"), 200)
        assert isinstance(reponse.json(), list), "le corps n'est pas une liste"

    @v.test("la liste contient le conteneur étiqueté, avec id, name, image et state")
    def _():
        liste = v.api("GET", "/containers", jeton="lecteur").json()
        trouves = [c for c in liste if c.get("name") == nom_gere]
        assert trouves, f"{nom_gere} (label {ETIQUETTE}=true) absent de la liste"
        conteneur = trouves[0]
        assert conteneur.get("id") == ids["gere"], "id différent de l'identifiant Docker complet"
        assert conteneur.get("image") == IMAGE, f"image reçue : {conteneur.get('image')}"
        assert conteneur.get("state") == "running", f"state reçu : {conteneur.get('state')}"

    @v.test("la liste ne montre pas les conteneurs sans label")
    def _():
        liste = v.api("GET", "/containers", jeton="lecteur").json()
        assert all(c.get("name") != nom_etranger for c in liste), (
            f"{nom_etranger} n'a pas le label {ETIQUETTE}=true et apparaît quand même"
        )

    @v.test("GET /containers/{id} répond 200 avec le bon conteneur")
    def _():
        reponse = attendu(v.api("GET", f"/containers/{ids['gere']}", jeton="lecteur"), 200)
        corps = reponse.json()
        assert corps.get("id") == ids["gere"] and corps.get("name") == nom_gere, (
            f"corps reçu : {reponse.text[:120]}"
        )
        assert corps.get("image") == IMAGE and corps.get("state") == "running", (
            f"corps reçu : {reponse.text[:120]}"
        )

    @v.test("GET /containers/{id} répond 404 pour un identifiant inconnu")
    def _():
        attendu(v.api("GET", "/containers/" + "0" * 64, jeton="lecteur"), 404)

    @v.test("GET /containers/{id} répond 404 pour un conteneur sans label")
    def _():
        attendu(v.api("GET", f"/containers/{ids['etranger']}", jeton="lecteur"), 404)


def bloc_ecriture(v):
    """TP4 : création, arrêt, démarrage, suppression, validation des entrées."""
    nom = v.nom("ecriture")
    cree = {}

    @v.test("POST /containers répond 201 avec le conteneur créé")
    def _():
        reponse = attendu(v.api("POST", "/containers", json={"name": nom, "image": IMAGE}), 201)
        cree.update(reponse.json())
        assert cree.get("name") == nom and cree.get("image") == IMAGE, (
            f"corps reçu : {reponse.text[:120]}"
        )
        assert cree.get("state") == "running", f"state reçu : {cree.get('state')}"

    @v.test("le conteneur existe réellement dans Docker, démarré et étiqueté")
    def _():
        constat = v.etat_docker(cree["id"])
        assert constat, "Docker ne connaît pas l'identifiant renvoyé"
        assert constat["state"] == "running", f"état Docker : {constat['state']}"
        assert constat["labels"].get(ETIQUETTE) == "true", f"label {ETIQUETTE}=true absent"

    @v.test("POST /containers répond 409 si le nom est déjà pris")
    def _():
        attendu(v.api("POST", "/containers", json={"name": nom, "image": IMAGE}), 409)

    @v.test("POST /containers répond 422 si le champ image manque")
    def _():
        attendu(v.api("POST", "/containers", json={"name": v.nom("incomplet")}), 422)

    @v.test("POST /containers répond 422 pour une image hors allowlist")
    def _():
        nom_refuse = v.nom("image")
        attendu(v.api("POST", "/containers", json={"name": nom_refuse, "image": "ubuntu:latest"}), 422)
        assert v.etat_docker(nom_refuse) is None, "le conteneur a été créé malgré le refus"

    @v.test("POST /containers répond 422 pour un nom invalide (majuscules, espace)")
    def _():
        attendu(v.api("POST", "/containers", json={"name": "Mon Conteneur", "image": IMAGE}), 422)

    @v.test("POST /containers/{id}/stop répond 200 et le conteneur est arrêté")
    def _():
        reponse = attendu(v.api("POST", f"/containers/{cree['id']}/stop"), 200)
        assert reponse.json().get("state") == "exited", f"state reçu : {reponse.json().get('state')}"
        assert v.etat_docker(cree["id"])["state"] == "exited", "Docker le voit encore démarré"

    @v.test("POST /containers/{id}/stop répond 409 s'il est déjà arrêté")
    def _():
        attendu(v.api("POST", f"/containers/{cree['id']}/stop"), 409)

    @v.test("POST /containers/{id}/start répond 200 et le conteneur redémarre")
    def _():
        reponse = attendu(v.api("POST", f"/containers/{cree['id']}/start"), 200)
        assert reponse.json().get("state") == "running", f"state reçu : {reponse.json().get('state')}"
        assert v.etat_docker(cree["id"])["state"] == "running", "Docker le voit encore arrêté"

    @v.test("POST /containers/{id}/start répond 409 s'il est déjà démarré")
    def _():
        attendu(v.api("POST", f"/containers/{cree['id']}/start"), 409)

    @v.test("POST /containers/{id}/stop répond 404 pour un identifiant inconnu")
    def _():
        assert cree.get("id"), "sans objet tant que la création échoue"
        attendu(v.api("POST", "/containers/" + "0" * 64 + "/stop"), 404)

    @v.test("DELETE /containers/{id} répond 204 et le conteneur disparaît de Docker")
    def _():
        reponse = attendu(v.api("DELETE", f"/containers/{cree['id']}"), 204)
        assert not reponse.content, "une réponse 204 n'a pas de corps"
        assert v.etat_docker(cree["id"]) is None, "Docker connaît encore le conteneur"

    @v.test("DELETE /containers/{id} répond 404 la seconde fois")
    def _():
        attendu(v.api("DELETE", f"/containers/{cree['id']}"), 404)


def bloc_authentification(v):
    """TP5 : 401 sans token valide, 403 avec le token lecteur sur une écriture."""
    nom = v.nom("auth")
    corps = {"name": nom, "image": IMAGE}
    protege = []

    @v.test("les trois secrets sont définis dans .env et tous différents")
    def _():
        secrets = [v.jetons["admin"], v.jetons["lecteur"], v.secret_webhook]
        assert all(secrets), "API_TOKEN_ADMIN, API_TOKEN_READER ou WEBHOOK_SECRET est vide"
        assert len(set(secrets)) == 3, "deux secrets ont la même valeur"

    @v.test("GET /health reste accessible sans token")
    def _():
        attendu(v.api("GET", "/health", jeton=None), 200)

    @v.test("GET /containers répond 401 sans token")
    def _():
        attendu(v.api("GET", "/containers", jeton=None), 401)
        protege.append(True)

    @v.test("GET /containers répond 401 avec un token inconnu")
    def _():
        attendu(v.api("GET", "/containers", jeton="inconnu"), 401)

    @v.test("GET /containers répond 200 avec le token lecteur")
    def _():
        assert protege, "sans objet tant que la route n'est pas protégée"
        attendu(v.api("GET", "/containers", jeton="lecteur"), 200)

    @v.test("POST /containers répond 401 sans token")
    def _():
        attendu(v.api("POST", "/containers", jeton=None, json=corps), 401)
        assert v.etat_docker(nom) is None, "le conteneur a été créé sans authentification"

    @v.test("POST /containers répond 403 avec le token lecteur")
    def _():
        attendu(v.api("POST", "/containers", jeton="lecteur", json=corps), 403)
        assert v.etat_docker(nom) is None, "le conteneur a été créé avec le rôle lecteur"

    @v.test("DELETE /containers/{id} répond 403 avec le token lecteur")
    def _():
        identifiant = v.lancer_directement(nom)
        attendu(v.api("DELETE", f"/containers/{identifiant}", jeton="lecteur"), 403)
        assert v.etat_docker(identifiant), "le conteneur a été supprimé avec le rôle lecteur"

    @v.test("POST /containers/{id}/stop répond 403 avec le token lecteur")
    def _():
        attendu(v.api("POST", f"/containers/{nom}/stop", jeton="lecteur"), 403)


def bloc_netbox(v):
    """TP6 : chaque conteneur géré a sa machine virtuelle dans le cluster docker-local."""
    nom = v.nom("netbox")
    cree = {}

    @v.test("NetBox est joignable avec le NETBOX_TOKEN du fichier .env")
    def _():
        assert v.netbox_token, "NETBOX_TOKEN est vide"
        reponse = v.netbox("GET", "/status/")
        assert reponse.status_code == 200, f"NetBox répond {reponse.status_code}"
        v.preparer_netbox()

    @v.test("créer un conteneur crée la machine virtuelle, statut active")
    def _():
        cree.update(attendu(v.api("POST", "/containers", json={"name": nom, "image": IMAGE}), 201).json())
        machine = v.machine_netbox(nom)
        assert machine, f"aucune machine virtuelle {nom} dans le cluster {CLUSTER}"
        assert machine["status"]["value"] == "active", f"statut : {machine['status']['value']}"

    @v.test("arrêter le conteneur passe la machine virtuelle en offline")
    def _():
        attendu(v.api("POST", f"/containers/{cree['id']}/stop"), 200)
        machine = v.machine_netbox(nom)
        assert machine and machine["status"]["value"] == "offline", (
            f"statut : {machine['status']['value'] if machine else 'machine absente'}"
        )

    @v.test("le redémarrer la repasse en active")
    def _():
        attendu(v.api("POST", f"/containers/{cree['id']}/start"), 200)
        machine = v.machine_netbox(nom)
        assert machine and machine["status"]["value"] == "active", (
            f"statut : {machine['status']['value'] if machine else 'machine absente'}"
        )

    @v.test("supprimer le conteneur supprime la machine virtuelle")
    def _():
        attendu(v.api("DELETE", f"/containers/{cree['id']}"), 204)
        assert v.machine_netbox(nom) is None, "la machine virtuelle existe encore dans NetBox"


def bloc_webhook(v):
    """TP7 : une alerte container_down redémarre le conteneur visé."""
    nom = v.nom("webhook")
    alerte = {"alert": "container_down", "container": nom}
    ids = {}

    def envoyer(corps, secret="bon"):
        entetes = {}
        if secret == "bon":
            entetes["X-Webhook-Secret"] = v.secret_webhook
        elif secret:
            entetes["X-Webhook-Secret"] = secret
        return v.api("POST", "/webhooks/alerts", jeton=None, json=corps, headers=entetes)

    @v.test("préparation : un conteneur géré est créé puis tombe en panne")
    def _():
        ids["id"] = v.lancer_directement(nom)
        v.docker("POST", f"/containers/{ids['id']}/stop", params={"t": 1})
        assert v.attendre_etat(ids["id"], "exited"), "impossible d'arrêter le conteneur de test"

    @v.test("POST /webhooks/alerts répond 401 sans header X-Webhook-Secret")
    def _():
        attendu(envoyer(alerte, secret=None), 401)

    @v.test("POST /webhooks/alerts répond 401 avec un secret faux, sans rien redémarrer")
    def _():
        attendu(envoyer(alerte, secret="pas-le-bon-secret"), 401)
        assert v.etat_docker(ids["id"])["state"] == "exited", "redémarré malgré un secret faux"

    @v.test("POST /webhooks/alerts répond 422 pour un type d'alerte inconnu")
    def _():
        attendu(envoyer({"alert": "disk_full", "container": nom}), 422)

    @v.test("une alerte valide répond 202 et le conteneur redémarre")
    def _():
        attendu(envoyer(alerte), 202)
        assert v.attendre_etat(ids["id"], "running"), "le conteneur n'a pas redémarré en 10 s"
        ids["remedie"] = True

    @v.test("POST /webhooks/alerts répond 404 pour un conteneur inconnu")
    def _():
        assert ids.get("remedie"), "sans objet tant que l'alerte valide échoue"
        attendu(envoyer({"alert": "container_down", "container": v.nom("fantome")}), 404)

    @v.test("la même alerte rejouée répond encore 202 (le conteneur tourne déjà)")
    def _():
        attendu(envoyer(alerte), 202)
        assert v.etat_docker(ids["id"])["state"] == "running", "le conteneur ne tourne plus"


def lire_metriques(texte):
    """Analyse le format texte Prometheus : {nom{labels}: valeur} et {nom: type}."""
    valeurs, types = {}, {}
    for ligne in texte.splitlines():
        ligne = ligne.strip()
        if ligne.startswith("# TYPE"):
            morceaux = ligne.split()
            if len(morceaux) >= 4:
                types[morceaux[2]] = morceaux[3]
        elif ligne and not ligne.startswith("#"):
            nom, _, valeur = ligne.rpartition(" ")
            try:
                valeurs[nom.replace(" ", "")] = float(valeur)
            except ValueError:
                pass
    return valeurs, types


OPERATIONS = [
    ("get", "/health"),
    ("get", "/containers"),
    ("post", "/containers"),
    ("get", "/containers/{}"),
    ("delete", "/containers/{}"),
    ("post", "/containers/{}/start"),
    ("post", "/containers/{}/stop"),
    ("post", "/webhooks/alerts"),
    ("get", "/metrics"),
]


def bloc_metriques(v):
    """TP8 : /metrics au format Prometheus, documentation OpenAPI."""
    nom = v.nom("metriques")
    releve = {}

    def relever():
        reponse = attendu(v.api("GET", "/metrics", jeton=None), 200)
        return reponse, *lire_metriques(reponse.text)

    def geres(etat):
        filtres = json.dumps({"label": [f"{ETIQUETTE}=true"], "status": [etat]})
        return len(v.docker("GET", "/containers/json", params={"all": "true", "filters": filtres}).json())

    @v.test("GET /metrics répond 200 en text/plain, sans token")
    def _():
        reponse, valeurs, types = relever()
        releve["avant"] = valeurs
        type_contenu = reponse.headers.get("Content-Type", "")
        assert type_contenu.startswith("text/plain"), f"Content-Type reçu : {type_contenu}"

    @v.test("adminapi_containers est déclarée gauge, adminapi_remediations_total counter")
    def _():
        _, _, types = relever()
        assert types.get("adminapi_containers") == "gauge", "ligne « # TYPE adminapi_containers gauge » absente"
        assert types.get("adminapi_remediations_total") == "counter", (
            "ligne « # TYPE adminapi_remediations_total counter » absente"
        )

    @v.test('adminapi_containers{state="running"} suit le nombre réel de conteneurs démarrés')
    def _():
        ident = v.lancer_directement(nom)
        releve["id"] = ident
        _, valeurs, _ = relever()
        vu = valeurs.get('adminapi_containers{state="running"}')
        assert vu is not None, 'série adminapi_containers{state="running"} absente'
        assert vu == geres("running"), f"valeur exposée {vu:g}, Docker en compte {geres('running')}"

    @v.test('adminapi_containers{state="exited"} suit le nombre réel de conteneurs arrêtés')
    def _():
        v.docker("POST", f"/containers/{releve['id']}/stop", params={"t": 1})
        v.attendre_etat(releve["id"], "exited")
        _, valeurs, _ = relever()
        vu = valeurs.get('adminapi_containers{state="exited"}')
        assert vu is not None, 'série adminapi_containers{state="exited"} absente'
        assert vu == geres("exited"), f"valeur exposée {vu:g}, Docker en compte {geres('exited')}"

    @v.test("adminapi_remediations_total augmente de 1 après une remédiation")
    def _():
        _, avant, _ = relever()
        entetes = {"X-Webhook-Secret": v.secret_webhook}
        corps = {"alert": "container_down", "container": nom}
        attendu(v.api("POST", "/webhooks/alerts", jeton=None, json=corps, headers=entetes), 202)
        _, apres, _ = relever()
        ecart = apres.get("adminapi_remediations_total", 0) - avant.get("adminapi_remediations_total", 0)
        assert "adminapi_remediations_total" in apres, "série adminapi_remediations_total absente"
        assert ecart == 1, f"le counter a varié de {ecart:g} au lieu de 1"

    @v.test("GET /docs et GET /openapi.json répondent 200")
    def _():
        attendu(v.api("GET", "/docs", jeton=None), 200)
        releve["openapi"] = attendu(v.api("GET", "/openapi.json", jeton=None), 200).json()

    def operations():
        """{(méthode, chemin normalisé): description de l'opération}"""
        trouvees = {}
        for chemin, methodes in releve["openapi"].get("paths", {}).items():
            morceaux = ["{}" if m.startswith("{") else m for m in chemin.split("/")]
            for methode, operation in methodes.items():
                trouvees[(methode.lower(), "/".join(morceaux))] = operation
        return trouvees

    @v.test("les 9 routes du contrat figurent dans la documentation")
    def _():
        manquantes = [f"{m.upper()} {c}" for m, c in OPERATIONS if (m, c) not in operations()]
        assert not manquantes, "routes absentes : " + ", ".join(manquantes)

    @v.test("chaque route porte un résumé (summary) rédigé")
    def _():
        connues = operations()
        sans = [
            f"{m.upper()} {c}"
            for m, c in OPERATIONS
            if (m, c) in connues and len(connues[(m, c)].get("summary", "")) < 10
        ]
        assert not sans, "résumé absent ou trop court : " + ", ".join(sans)
        assert all(cle in connues for cle in OPERATIONS), "des routes manquent"

    @v.test("les codes d'erreur sont documentés (401, 403, 404, 409)")
    def _():
        connues = operations()
        attendus = {
            ("get", "/containers"): ["401"],
            ("get", "/containers/{}"): ["401", "404"],
            ("post", "/containers"): ["201", "401", "403", "409", "422"],
            ("delete", "/containers/{}"): ["204", "401", "403", "404"],
            ("post", "/containers/{}/stop"): ["401", "403", "404", "409"],
            ("post", "/containers/{}/start"): ["401", "403", "404", "409"],
            ("post", "/webhooks/alerts"): ["202", "401", "404"],
        }
        oublis = []
        for (methode, chemin), codes in attendus.items():
            documentes = connues.get((methode, chemin), {}).get("responses", {})
            for code in codes:
                if code not in documentes:
                    oublis.append(f"{methode.upper()} {chemin} : {code}")
        assert not oublis, "non documentés : " + " ; ".join(oublis[:6])


BLOCS = {
    "lecture": bloc_lecture,
    "ecriture": bloc_ecriture,
    "authentification": bloc_authentification,
    "netbox": bloc_netbox,
    "webhook": bloc_webhook,
    "metriques": bloc_metriques,
}


def executer(verificateur, noms, blocs=BLOCS):
    """Joue les blocs demandés. Renvoie {bloc: [(libellé, réussi, détail)]}."""
    verificateur.nettoyer()
    if verificateur.netbox_token:
        try:
            verificateur.preparer_netbox()  # le cluster doit exister avant toute création
        except requests.RequestException:
            pass  # NetBox injoignable : le bloc netbox le signalera
    try:
        for nom in noms:
            verificateur.bloc = nom
            blocs[nom](verificateur)
    finally:
        verificateur.nettoyer()
    return verificateur.resultats


def afficher(resultats):
    tout_passe = True
    for bloc, constats in resultats.items():
        reussis = sum(1 for _, ok, _ in constats if ok)
        print(f"\n== {bloc} : {reussis}/{len(constats)} ==")
        for libelle, ok, detail in constats:
            print(f"  [{'ok' if ok else 'KO'}] {libelle}")
            if not ok:
                print(f"         -> {detail}")
                tout_passe = False
    total = sum(len(c) for c in resultats.values())
    reussis = sum(1 for c in resultats.values() for _, ok, _ in c if ok)
    print(f"\nTotal : {reussis}/{total} vérifications passées.")
    return tout_passe


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    analyseur = argparse.ArgumentParser(description="Vérifie une micro-API d'administration.")
    analyseur.add_argument("blocs", nargs="*", help="blocs à jouer (tous par défaut)")
    analyseur.add_argument("--api", default="http://127.0.0.1:8000", help="URL de votre API")
    analyseur.add_argument("--env", default=".env", help="fichier de configuration à lire")
    arguments = analyseur.parse_args()

    inconnus = [b for b in arguments.blocs if b not in BLOCS]
    if inconnus:
        analyseur.error(f"bloc inconnu : {', '.join(inconnus)}. Blocs : {', '.join(BLOCS)}")

    verificateur = Verificateur(arguments.api, lire_env(arguments.env))
    try:
        verificateur.docker("GET", "/version").raise_for_status()
    except requests.RequestException:
        sys.exit(
            f"L'API Docker ne répond pas sur {verificateur.docker_url}.\n"
            "Lancez l'environnement : docker compose up -d"
        )
    resultats = executer(verificateur, arguments.blocs or list(BLOCS))
    sys.exit(0 if afficher(resultats) else 1)


if __name__ == "__main__":
    main()
