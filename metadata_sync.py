"""
Script 1 — metadata-sync
=========================
Détecte quand un Terme du Glossaire est mis à jour.
Recherche toutes les entrées liées via `lookupEntryLinks` et copie 
l'intégralité des Aspects personnalisés vers chaque entrée technique liée.

Point d'entrée (Entry point) : universal_glossary_sync
"""

import base64
import json
import logging
import functions_framework
from google.cloud import dataplex_v1
from google.cloud.dataplex_v1.types import GetEntryRequest, EntryView
from google.protobuf import field_mask_pb2
import google.auth
import google.auth.transport.requests
import urllib.request
import urllib.parse
import urllib.error

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
dataplex_client = dataplex_v1.CatalogServiceClient()

# ⚠️ À MODIFIER : Insérez l'ID de votre projet Google Cloud
PROJECT_ID = "VOTRE_ID_DE_PROJET"

# Les aspects système de Dataplex (Ne doivent jamais être copiés manuellement)
SYSTEM_ASPECT_KEYWORDS = [
    "generic", "overview", "glossary-term-aspect",
    "schema", "resource", "linked_resources",
]

# Cache pour éviter les boucles infinies dues aux retries d'Eventarc
_seen_events = set()
_MAX_CACHE = 500

def _is_system_aspect(key):
    return any(kw in key for kw in SYSTEM_ASPECT_KEYWORDS)

def _is_self_triggered(proto_payload):
    """Vérifie si la fonction s'est déclenchée elle-même pour éviter une boucle infinie."""
    caller = proto_payload.get("authenticationInfo", {}).get("principalEmail", "")
    if not caller:
        return False
    return ("compute@developer.gserviceaccount.com" in caller
            or "cloudfunctions" in caller)

def _get_access_token():
    credentials, _ = google.auth.default()
    auth_req = google.auth.transport.requests.Request()
    credentials.refresh(auth_req)
    return credentials.token

def lookup_linked_entries(glossary_term_name, project_number, location):
    """Recherche toutes les entrées liées à un terme du glossaire via l'API REST."""
    token = _get_access_token()
    linked = []

    # Encodage sécurisé de l'URL
    encoded_entry = urllib.parse.quote(glossary_term_name, safe="/")

    # On teste l'ID du projet en premier, puis le numéro du projet
    for project in [PROJECT_ID, project_number]:
        url = (
            f"https://dataplex.googleapis.com/v1/"
            f"projects/{project}/locations/{location}"
            f":lookupEntryLinks?entry={encoded_entry}"
        )

        logger.info(f"    → Appel de lookupEntryLinks ({project}/{location})")

        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {token}")

        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read().decode("utf-8")
                data = json.loads(raw)
        except urllib.error.HTTPError as e:
            logger.warning(f"    Erreur HTTP {e.code}")
            continue
        except Exception as e:
            logger.warning(f"    Erreur: {e}")
            continue

        for entry_link in data.get("entryLinks", []):
            refs = entry_link.get("entryReferences", [])
            for ref in refs:
                ref_name = ref.get("name", "")
                ref_type = ref.get("type", "")

                # On ignore le terme source et les synonymes
                if ref_name == glossary_term_name or ref_type == "TARGET":
                    continue
                if "/glossaries/" in ref_name and "/terms/" in ref_name:
                    continue

                linked.append(ref_name)

        if linked:
            logger.info(f"    ✅ {len(linked)} entrées liées trouvées")
            return linked

    return linked

def apply_aspects(target_entry_name, aspects_to_apply):
    """Applique les aspects personnalisés sur l'entrée cible (ex: BQ Table)."""
    try:
        req = GetEntryRequest(name=target_entry_name, view=EntryView.FULL)
        target_entry = dataplex_client.get_entry(request=req)
    except Exception as e:
        logger.warning(f"⚠️ Impossible de récupérer l'entrée {target_entry_name}: {e}")
        return

    keys_to_update = []
    for key, data in aspects_to_apply.items():
        if _is_system_aspect(key):
            continue
        if key not in target_entry.aspects or target_entry.aspects[key] != data:
            target_entry.aspects[key] = data
            keys_to_update.append(key)

    if not keys_to_update:
        logger.info(f"ℹ️  Déjà synchronisé : {target_entry_name}")
        return

    update_request = dataplex_v1.UpdateEntryRequest(
        entry=target_entry,
        update_mask=field_mask_pb2.FieldMask(paths=["aspects"]),
        aspect_keys=keys_to_update,
    )
    dataplex_client.update_entry(request=update_request)
    logger.info(f"✅ Synchronisé {keys_to_update} → {target_entry_name}")

@functions_framework.cloud_event
def universal_glossary_sync(cloud_event):
    event_id = getattr(cloud_event, "id", None) or cloud_event.get("id", "")
    if event_id and event_id in _seen_events:
        return
    if event_id:
        _seen_events.add(event_id)
        if len(_seen_events) > _MAX_CACHE:
            _seen_events.clear()

    try:
        log_data = cloud_event.data
        if "message" in cloud_event.data:
            log_data = json.loads(base64.b64decode(cloud_event.data["message"]["data"]).decode("utf-8"))

        proto_payload = log_data.get("protoPayload", {})
        if "UpdateEntry" not in proto_payload.get("methodName", ""):
            return

        if _is_self_triggered(proto_payload):
            return

        entry_name = proto_payload.get("resourceName")
        if not entry_name:
            return

        try:
            req = GetEntryRequest(name=entry_name, view=EntryView.FULL)
            entry = dataplex_client.get_entry(request=req)
        except Exception as e:
            return

        entry_type = getattr(entry, "entry_type", "").lower()
        if not any(kw in entry_type for kw in ["glossary", "category", "term"]):
            return

        custom_aspects = {k: v for k, v in entry.aspects.items() if not _is_system_aspect(k)}
        if not custom_aspects:
            return

        logger.info(f"PROPAGATION: Depuis {entry_name}")

        parts = entry_name.split("/")
        project_number = parts[1]
        location = parts[3]

        linked = lookup_linked_entries(entry_name, project_number, location)

        for target in linked:
            apply_aspects(target, custom_aspects)

    except Exception as e:
        logger.error(f"❌ {e}", exc_info=True)
        return
