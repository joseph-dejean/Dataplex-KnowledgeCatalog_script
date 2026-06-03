"""
Script 3 — mask
================
Quand un Aspect lié au masquage est ajouté manuellement ou via le sync
sur une colonne BigQuery dans Dataplex, ce script lit la valeur, la map
avec un Policy Tag, et l'applique directement sur le schéma BigQuery.

Entry Point : enforce_bq_policy_tags
"""

import json
import base64
import logging
import functions_framework
from google.cloud import bigquery
from google.cloud import dataplex_v1
from google.protobuf.json_format import MessageToDict

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bq_client = bigquery.Client()
dataplex_client = dataplex_v1.CatalogServiceClient()

# =====================================================================
# DICTIONNAIRE DE MAPPING (Valeur Dataplex -> Policy Tag BigQuery)
# ⚠️ À MODIFIER : Insérez les vraies valeurs et IDs de votre organisation
# =====================================================================
POLICY_TAG_MAP = {
    "masking": "projects/VOTRE_PROJET/locations/VOTRE_REGION/taxonomies/ID_TAXONOMIE/policyTags/ID_TAG_MASKING",
    "last_4": "projects/VOTRE_PROJET/locations/VOTRE_REGION/taxonomies/ID_TAXONOMIE/policyTags/ID_TAG_LAST4",
    "encrypt": "projects/VOTRE_PROJET/locations/VOTRE_REGION/taxonomies/ID_TAXONOMIE/policyTags/ID_TAG_ENCRYPT"
}
# =====================================================================

_seen_events = set()
_MAX_CACHE = 500

def _is_self_triggered(proto_payload):
    """Filtre les events générés par le script lui-même pour éviter les boucles."""
    caller = proto_payload.get("authenticationInfo", {}).get("principalEmail", "")
    return ("compute@developer.gserviceaccount.com" in caller or "cloudfunctions" in caller)

@functions_framework.cloud_event
def enforce_bq_policy_tags(cloud_event):
    # Deduplication cache
    event_id = getattr(cloud_event, "id", None) or cloud_event.get("id", "")
    if event_id and event_id in _seen_events:
        return
    if event_id:
        _seen_events.add(event_id)
        if len(_seen_events) > _MAX_CACHE:
            _seen_events.clear()

    try:
        # 1. Parse du payload Eventarc
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

        logger.info(f"🔔 Event détecté sur : {entry_name}")

        if "bigquery.googleapis.com" not in entry_name:
            logger.info("ℹ️ L'asset n'est pas une table BigQuery. Skipping.")
            return

        # 2. On fetch la table complète en direct depuis Dataplex
        try:
            entry = dataplex_client.get_entry(name=entry_name)
        except Exception as e:
            logger.warning(f"⚠️ Impossible de fetch l'entry : {e}")
            return

        columns_to_mask = {}

        # 3. On cherche les Aspects qui ciblent spécifiquement des colonnes
        for aspect_key, aspect_data in entry.aspects.items():
            # Dans Dataplex, les aspects de colonne contiennent toujours un "@"
            if "@" not in aspect_key:
                continue 
                
            column_name = aspect_key.split("@")[-1]
            
            # On convertit les data Protobuf en JSON string pour faire un full-text search
            aspect_dict = MessageToDict(aspect_data._pb)
            aspect_json_string = json.dumps(aspect_dict)

            # On check si l'une de nos rules ("masking", "last_4"...) est dans le JSON
            for user_value, tag_id in POLICY_TAG_MAP.items():
                if f'"{user_value}"' in aspect_json_string:
                    columns_to_mask[column_name] = tag_id
                    logger.info(f"🎯 MATCH : La colonne '{column_name}' match avec la rule '{user_value}'")
                    break

        if not columns_to_mask:
            logger.info("ℹ️ Aucun aspect de sécurité trouvé sur les colonnes. Skipping.")
            return 

        # 4. Extraction des coordonnées BigQuery depuis l'URL
        try:
            bq_path = entry_name.split("bigquery.googleapis.com/")[1]
            parts = bq_path.split("/")
            project_id = parts[1]
            dataset_id = parts[3]
            table_id = parts[5]
        except (ValueError, IndexError):
            logger.warning(f"⚠️ Impossible de parser l'URL BQ : {entry_name}")
            return

        table_ref = f"{project_id}.{dataset_id}.{table_id}"
        
        # 5. Patch du schéma BigQuery
        try:
            table = bq_client.get_table(table_ref)
        except Exception as e:
            logger.error(f"❌ Impossible de fetch la table BQ : {e}")
            return

        new_schema = []
        schema_updated = False

        for field in table.schema:
            if field.name in columns_to_mask:
                policy_tag_id = columns_to_mask[field.name]
                
                # Check si le tag est déjà là pour éviter les API calls inutiles
                current_tags = field.policy_tags.names if field.policy_tags else []
                if policy_tag_id in current_tags:
                    new_schema.append(field)
                    continue
                    
                policy_tags = bigquery.PolicyTagList(names=[policy_tag_id])
                new_field = bigquery.SchemaField(
                    name=field.name,
                    field_type=field.field_type,
                    mode=field.mode,
                    description=field.description,
                    fields=field.fields,
                    policy_tags=policy_tags
                )
                new_schema.append(new_field)
                schema_updated = True
            else:
                new_schema.append(field)

        if schema_updated:
            table.schema = new_schema
            bq_client.update_table(table, ["schema"])
            logger.info(f"✅ SUCCESS : Policy Tags appliqués sur {table_ref} (Colonnes: {list(columns_to_mask.keys())})")
        else:
            logger.info("ℹ️ Le schéma BigQuery est déjà à jour.")

    except Exception as e:
        logger.error(f"❌ Erreur lors de l'application du tag : {str(e)}", exc_info=True)
        raise e
