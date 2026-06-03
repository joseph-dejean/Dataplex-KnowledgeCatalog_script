"""
Script 2 — term-sync
======================
Lorsqu'un terme du glossaire est lié manuellement à un asset BigQuery,
ce script "aspire" les aspects du glossaire vers l'entrée BigQuery ET 
applique immédiatement le Policy Tag si une règle de masquage est détectée.

Point d'entrée : sync_term_aspects
"""

import base64
import json
import logging
import functions_framework
from google.cloud import dataplex_v1
from google.cloud import bigquery
from google.cloud.dataplex_v1.types import GetEntryRequest, EntryView
from google.protobuf import field_mask_pb2
import google.auth
import google.auth.transport.requests
import urllib.request
import urllib.parse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
dataplex_client = dataplex_v1.CatalogServiceClient()
bq_client = bigquery.Client()

# ⚠️ À MODIFIER : Insérez l'ID de votre projet Google Cloud
PROJECT_ID = "VOTRE_ID_DE_PROJET"

SYSTEM_ASPECT_KEYWORDS = [
    "generic", "overview", "glossary-term-aspect",
    "schema", "resource", "linked_resources",
]

# =====================================================================
# DICTIONNAIRE DE MAPPING (Aspect -> Policy Tag BigQuery)
# ⚠️ À MODIFIER par l'équipe Data : Remplacez par vos propres IDs de Tags
# =====================================================================
ASPECT_TO_POLICY_TAG = {
    "masking": "projects/VOTRE_PROJET/locations/VOTRE_REGION/taxonomies/ID_TAXONOMIE/policyTags/ID_TAG_MASKING",
    "last_4": "projects/VOTRE_PROJET/locations/VOTRE_REGION/taxonomies/ID_TAXONOMIE/policyTags/ID_TAG_LAST4",
    "encrypt": "projects/VOTRE_PROJET/locations/VOTRE_REGION/taxonomies/ID_TAXONOMIE/policyTags/ID_TAG_ENCRYPT",
}

_seen_events = set()
_MAX_CACHE = 500

def _is_system_aspect(key):
    return any(kw in key for kw in SYSTEM_ASPECT_KEYWORDS)

def _is_self_triggered(proto_payload):
    caller = proto_payload.get("authenticationInfo", {}).get("principalEmail", "")
    if not caller:
        return False
    return ("compute@developer.gserviceaccount.com" in caller or "cloudfunctions" in caller)

def _get_access_token():
    credentials, _ = google.auth.default()
    auth_req = google.auth.transport.requests.Request()
    credentials.refresh(auth_req)
    return credentials.token

def _parse_aspect_type(aspect_key):
    """Extrait le type d'aspect depuis la clé Dataplex"""
    base = aspect_key.split("@")[0] if "@" in aspect_key else aspect_key
    parts = base.split(".")
    if len(parts) >= 3:
        return ".".join(parts[2:])
    return None

def lookup_linked_terms(bq_entry_name, project_number, location):
    token = _get_access_token()
    linked_terms = []
    encoded_entry = urllib.parse.quote(bq_entry_name, safe="/")

    for project in [PROJECT_ID, project_number]:
        url = (
            f"https://dataplex.googleapis.com/v1/"
            f"projects/{project}/locations/{location}"
            f":lookupEntryLinks?entry={encoded_entry}"
        )

        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {token}")

        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            continue

        for entry_link in data.get("entryLinks", []):
            refs = entry_link.get("entryReferences", [])
            for ref in refs:
                ref_name = ref.get("name", "")
                ref_path = ref.get("path", "")
                if ref_name == bq_entry_name:
                    continue
                if "/glossaries/" in ref_name and "/terms/" in ref_name:
                    column = ref_path.replace("Schema.", "") if ref_path else None
                    linked_terms.append({"term": ref_name, "column": column})

        if linked_terms:
            return linked_terms
    return linked_terms

@functions_framework.cloud_event
def sync_term_aspects(cloud_event):
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
        method_name = proto_payload.get("methodName", "")

        if "CreateEntryLink" not in method_name and "UpdateEntry" not in method_name:
            return

        if _is_self_triggered(proto_payload):
            return

        if "CreateEntryLink" in method_name:
            request_data = proto_payload.get("request", {})
            response_data = proto_payload.get("response", {})
            entry_link = (
                request_data.get("entryLink", {}) or request_data.get("entry_link", {})
                or response_data.get("entryLink", {}) or response_data.get("entry_link", {})
                or response_data
            )
            refs = entry_link.get("entryReferences", []) or entry_link.get("entry_references", [])

            bq_entry_name, term_name, column = None, None, None

            for ref in refs:
                ref_name = ref.get("name", "")
                ref_type = ref.get("type", "")
                ref_path = ref.get("path", "")

                if "bigquery" in ref_name.lower() or ref_type == "SOURCE":
                    bq_entry_name = ref_name
                    if ref_path:
                        column = ref_path.replace("Schema.", "")
                elif "/glossaries/" in ref_name and "/terms/" in ref_name:
                    term_name = ref_name

            if not bq_entry_name or not term_name:
                return

            entry_name = bq_entry_name
            direct_terms = [{"term": term_name, "column": column}]

        else:
            entry_name = proto_payload.get("resourceName", "")
            if not entry_name or "bigquery" not in entry_name.lower():
                return
            direct_terms = None 

        if direct_terms:
            linked_terms = direct_terms
        else:
            parts = entry_name.split("/")
            project_number, location = parts[1], parts[3]
            linked_terms = lookup_linked_terms(entry_name, project_number, location)
        
        if not linked_terms:
            return

        try:
            req = GetEntryRequest(name=entry_name, view=EntryView.FULL)
            bq_entry = dataplex_client.get_entry(request=req)
        except Exception as e:
            return

        keys_to_update = []
        columns_to_mask = {} 

        for link in linked_terms:
            term_name = link["term"]
            column = link.get("column")

            try:
                term_req = GetEntryRequest(name=term_name, view=EntryView.FULL)
                term_entry = dataplex_client.get_entry(request=term_req)
            except Exception:
                continue

            for aspect_key, aspect_data in term_entry.aspects.items():
                if _is_system_aspect(aspect_key):
                    continue

                target_key = f"{aspect_key}@Schema.{column}" if column else aspect_key

                if target_key not in bq_entry.aspects or bq_entry.aspects[target_key] != aspect_data:
                    bq_entry.aspects[target_key] = aspect_data
                    keys_to_update.append(target_key)

                aspect_type = _parse_aspect_type(aspect_key)
                if aspect_type and aspect_type in ASPECT_TO_POLICY_TAG and column:
                    columns_to_mask[column] = ASPECT_TO_POLICY_TAG[aspect_type]

        if keys_to_update:
            update_request = dataplex_v1.UpdateEntryRequest(
                entry=bq_entry,
                update_mask=field_mask_pb2.FieldMask(paths=["aspects"]),
                aspect_keys=keys_to_update,
            )
            dataplex_client.update_entry(request=update_request)
            logger.info(f"✅ Synchronisation Dataplex réussie.")

        if columns_to_mask:
            bq_part = entry_name.split("bigquery.googleapis.com/")[1]
            bq_parts = bq_part.split("/")
            table_ref = f"{bq_parts[1]}.{bq_parts[3]}.{bq_parts[5]}"

            table = bq_client.get_table(table_ref)
            new_schema = []
            schema_changed = False

            for field in table.schema:
                if field.name in columns_to_mask:
                    tag_id = columns_to_mask[field.name]
                    existing_tags = field.policy_tags.names if field.policy_tags else []

                    if tag_id not in existing_tags:
                        new_schema.append(bigquery.SchemaField(
                            name=field.name, field_type=field.field_type, mode=field.mode,
                            description=field.description, fields=field.fields,
                            policy_tags=bigquery.PolicyTagList(names=[tag_id]),
                        ))
                        schema_changed = True
                    else:
                        new_schema.append(field)
                else:
                    new_schema.append(field)

            if schema_changed:
                table.schema = new_schema
                bq_client.update_table(table, ["schema"])
                logger.info(f"✅ Policy tags appliqués sur BigQuery ({table_ref})")

    except Exception as e:
        logger.error(f"❌ {e}", exc_info=True)
        return
