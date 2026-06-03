"""
Script 4 — unmask
==================
Lorsqu'un Aspect de masquage est RETIRÉ d'une colonne BQ dans Dataplex,
ce script supprime le Policy Tag BigQuery correspondant pour libérer l'accès.

Point d'entrée : remove_bq_policy_tags
"""

import json
import base64
import logging
import functions_framework
from google.cloud import bigquery
from google.cloud import dataplex_v1
from google.cloud.dataplex_v1.types import GetEntryRequest, EntryView

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bq_client = bigquery.Client()
dataplex_client = dataplex_v1.CatalogServiceClient()

# =====================================================================
# ⚠️ À MODIFIER : Liste des Aspects qui représentent une règle de masquage
# =====================================================================
MASKING_ASPECT_TYPES = {"masking", "masking-rule", "mask", "encrypt", "last-4", "last_4"}

# ⚠️ À MODIFIER : Tous les Policy Tags gérés par ce système automatisé.
# Seuls CES tags seront retirés (pour éviter de casser une sécurité manuelle).
KNOWN_POLICY_TAGS = {
    "projects/VOTRE_PROJET/locations/VOTRE_REGION/taxonomies/ID_TAXONOMIE/policyTags/ID_TAG_MASKING",
    "projects/VOTRE_PROJET/locations/VOTRE_REGION/taxonomies/ID_TAXONOMIE/policyTags/ID_TAG_LAST4",
    "projects/VOTRE_PROJET/locations/VOTRE_REGION/taxonomies/ID_TAXONOMIE/policyTags/ID_TAG_ENCRYPT",
}
# =====================================================================

_seen_events = set()
_MAX_CACHE = 500

def _is_self_triggered(proto_payload):
    caller = proto_payload.get("authenticationInfo", {}).get("principalEmail", "")
    return ("compute@developer.gserviceaccount.com" in caller or "cloudfunctions" in caller)

def _parse_aspect_key(aspect_key):
    if "@" not in aspect_key:
        return None, None
    base, path = aspect_key.split("@", 1)
    parts = base.split(".")
    if len(parts) < 3:
        return None, None
    aspect_type = ".".join(parts[2:])
    column_name = path.replace("Schema.", "")
    return aspect_type, column_name

@functions_framework.cloud_event
def remove_bq_policy_tags(cloud_event):
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

        entry_name = proto_payload.get("resourceName", "")
        if not entry_name or "bigquery" not in entry_name.lower():
            return

        logger.info(f"🔔 Événement détecté sur : {entry_name}")

        try:
            req = GetEntryRequest(name=entry_name, view=EntryView.FULL)
            entry = dataplex_client.get_entry(request=req)
        except Exception as e:
            logger.warning(f"⚠️ Impossible de récupérer l'entrée : {e}")
            return

        # Trouve les colonnes qui possèdent ENCORE un aspect de masquage
        columns_with_masking = set()
        for aspect_key in entry.aspects:
            aspect_type, column_name = _parse_aspect_key(aspect_key)
            if aspect_type and column_name and aspect_type in MASKING_ASPECT_TYPES:
                columns_with_masking.add(column_name)

        try:
            bq_part = entry_name.split("bigquery.googleapis.com/")[1]
            parts = bq_part.split("/")
            project_id = parts[parts.index("projects") + 1]
            dataset_id = parts[parts.index("datasets") + 1]
            table_id = parts[parts.index("tables") + 1]
        except (ValueError, IndexError):
            return

        table_ref = f"{project_id}.{dataset_id}.{table_id}"
        table = bq_client.get_table(table_ref)

        new_schema = []
        schema_changed = False
        removed_columns = []

        # Application de la suppression si nécessaire
        for field in table.schema:
            existing_tags = field.policy_tags.names if field.policy_tags else []
            managed_tags = [t for t in existing_tags if t in KNOWN_POLICY_TAGS]

            # Si la colonne possède un Tag géré par l'automatisation, 
            # mais que Dataplex ne contient plus l'Aspect -> on supprime le Tag !
            if managed_tags and field.name not in columns_with_masking:
                remaining_tags = [t for t in existing_tags if t not in KNOWN_POLICY_TAGS]

                field_config = field.to_api_repr()
                field_config["policyTags"] = {"names": remaining_tags}
                new_schema.append(bigquery.SchemaField.from_api_repr(field_config))
                
                schema_changed = True
                removed_columns.append(field.name)
            else:
                new_schema.append(field)

        if schema_changed:
            table.schema = new_schema
            bq_client.update_table(table, ["schema"])
            logger.info(f"✅ SUCCÈS : Policy tags RETIRÉS de {table_ref} sur les colonnes : {removed_columns}")
        else:
            logger.info("ℹ️ Aucun tag à supprimer.")

    except Exception as e:
        logger.error(f"❌ {e}", exc_info=True)
        return
