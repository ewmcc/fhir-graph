"""
FHIR Bundle -> Neo4j Loader
----------------------------
Reads a single Synthea FHIR R4 Bundle (one patient) and loads it into Neo4j as a
property graph. Each FHIR resource becomes a labeled node; FHIR references become
named relationships (e.g. FOR_PATIENT, DURING_ENCOUNTER).

The bundle JSON is parsed with the standard library (json module) using raw dict
access against the FHIR R4 JSON field names. This avoids any dependency on a
specific version of fhir.resources and works with any Synthea R4 export.

Covered resource types
  Patient . Encounter . Condition . Observation
  Procedure . MedicationRequest . Medication . Immunization
"""

import json
from neo4j import GraphDatabase

# --- Config -------------------------------------------------------------------

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "password"

FHIR_FILE = "synthea-fhir/Addie421_Isabelle619_Johns824_c72a8c6e-1dfd-0a8b-20e7-066eadd4931c.json"

# --- Helpers ------------------------------------------------------------------

def ref_id(reference_string):
    """Return the bare UUID from a FHIR reference.

    "urn:uuid:abc-123"  ->  "abc-123"
    "Patient/abc-123"   ->  "abc-123"
    """
    if not reference_string:
        return None
    if reference_string.startswith("urn:uuid:"):
        return reference_string[len("urn:uuid:"):]
    if "/" in reference_string:
        return reference_string.split("/")[-1]
    return reference_string


def get_ref(resource, *keys):
    """Safely walk a chain of dict keys and call ref_id on the final 'reference' value."""
    obj = resource
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
        if obj is None:
            return None
    if isinstance(obj, dict):
        return ref_id(obj.get("reference"))
    return None


# --- Resource handlers --------------------------------------------------------
# Each handler returns (label, props_dict, refs_list).
# refs_list entries are (rel_type, target_label, target_id).

def handle_patient(r):
    name_list = r.get("name") or []
    first = name_list[0] if name_list else {}
    given = " ".join(first.get("given") or []) or None
    family = first.get("family")
    full_name = f"{given} {family}" if given and family else None

    addr_list = r.get("address") or []
    addr = addr_list[0] if addr_list else {}

    telecom = r.get("telecom") or []
    phone = telecom[0].get("value") if telecom else None

    props = {
        "id": r.get("id"),
        "name": full_name,
        "gender": r.get("gender"),
        "birthDate": r.get("birthDate"),
        "city": addr.get("city"),
        "state": addr.get("state"),
        "phone": phone,
    }
    return "Patient", props, []


def handle_encounter(r):
    # Synthea R4 emits "class" as a single Coding object; R4B changed it to a list.
    class_field = r.get("class") or {}
    if isinstance(class_field, list):
        class_code = class_field[0].get("code") if class_field else None
    else:
        class_code = class_field.get("code")

    type_list = r.get("type") or []
    encounter_type = type_list[0].get("text") if type_list else None

    period = r.get("period") or {}
    props = {
        "id": r.get("id"),
        "status": r.get("status"),
        "class": class_code,
        "type": encounter_type,
        "start": period.get("start"),
        "end": period.get("end"),
    }
    refs = [
        ("FOR_PATIENT", "Patient", get_ref(r, "subject")),
    ]
    return "Encounter", props, refs


def handle_condition(r):
    clinical_status = r.get("clinicalStatus") or {}
    codings = clinical_status.get("coding") or []
    status_code = codings[0].get("code") if codings else None

    props = {
        "id": r.get("id"),
        "display": (r.get("code") or {}).get("text"),
        "onset": r.get("onsetDateTime"),
        "clinicalStatus": status_code,
    }
    refs = [
        ("FOR_PATIENT", "Patient", get_ref(r, "subject")),
        ("DURING_ENCOUNTER", "Encounter", get_ref(r, "encounter")),
    ]
    return "Condition", props, refs


def handle_observation(r):
    vq = r.get("valueQuantity")
    vcc = r.get("valueCodeableConcept")
    vs = r.get("valueString")

    if vq:
        value = str(vq.get("value"))
        unit = vq.get("unit")
    elif vcc:
        value = vcc.get("text")
        unit = None
    elif vs:
        value = vs
        unit = None
    else:
        value = None
        unit = None

    props = {
        "id": r.get("id"),
        "display": (r.get("code") or {}).get("text"),
        "date": r.get("effectiveDateTime"),
        "status": r.get("status"),
        "value": value,
        "unit": unit,
    }
    refs = [
        ("FOR_PATIENT", "Patient", get_ref(r, "subject")),
        ("DURING_ENCOUNTER", "Encounter", get_ref(r, "encounter")),
    ]
    return "Observation", props, refs


def handle_procedure(r):
    pp = r.get("performedPeriod") or {}
    start = pp.get("start") or r.get("performedDateTime")

    props = {
        "id": r.get("id"),
        "display": (r.get("code") or {}).get("text"),
        "status": r.get("status"),
        "start": start,
    }
    refs = [
        ("FOR_PATIENT", "Patient", get_ref(r, "subject")),
        ("DURING_ENCOUNTER", "Encounter", get_ref(r, "encounter")),
    ]
    return "Procedure", props, refs


def handle_medication(r):
    props = {
        "id": r.get("id"),
        "display": (r.get("code") or {}).get("text"),
        "status": r.get("status"),
    }
    return "Medication", props, []


def handle_medication_request(r):
    props = {
        "id": r.get("id"),
        "status": r.get("status"),
        "intent": r.get("intent"),
        "authoredOn": r.get("authoredOn"),
    }
    refs = [
        ("FOR_PATIENT", "Patient", get_ref(r, "subject")),
        ("DURING_ENCOUNTER", "Encounter", get_ref(r, "encounter")),
        ("PRESCRIBED_MEDICATION", "Medication", get_ref(r, "medicationReference")),
    ]
    return "MedicationRequest", props, refs


def handle_immunization(r):
    props = {
        "id": r.get("id"),
        "vaccine": (r.get("vaccineCode") or {}).get("text"),
        "date": r.get("occurrenceDateTime"),
        "status": r.get("status"),
    }
    refs = [
        # Immunization uses .patient, not .subject (per FHIR R4 spec)
        ("FOR_PATIENT", "Patient", get_ref(r, "patient")),
        ("DURING_ENCOUNTER", "Encounter", get_ref(r, "encounter")),
    ]
    return "Immunization", props, refs


RESOURCE_HANDLERS = {
    "Patient": handle_patient,
    "Encounter": handle_encounter,
    "Condition": handle_condition,
    "Observation": handle_observation,
    "Procedure": handle_procedure,
    "Medication": handle_medication,
    "MedicationRequest": handle_medication_request,
    "Immunization": handle_immunization,
}

# --- Generic Neo4j write helpers ----------------------------------------------

def write_node(session, label, props):
    """MERGE a node by id and set all remaining properties."""
    set_clause = ", ".join(f"n.{k} = ${k}" for k in props if k != "id")
    query = f"MERGE (n:{label} {{id: $id}}) SET {set_clause}"
    session.run(query, **props)


def write_rel(session, src_label, src_id, rel_type, tgt_label, tgt_id):
    """MERGE a directed relationship between two nodes."""
    query = (
        f"MATCH (src:{src_label} {{id: $src_id}}) "
        f"MATCH (tgt:{tgt_label} {{id: $tgt_id}}) "
        f"MERGE (src)-[:{rel_type}]->(tgt)"
    )
    session.run(query, src_id=src_id, tgt_id=tgt_id)


# --- Load & connect -----------------------------------------------------------

print(f"[LOAD] Reading {FHIR_FILE}")
with open(FHIR_FILE, "r") as f:
    data = json.load(f)

entries = data.get("entry", [])
print(f"  {len(entries)} entries in bundle")

print(f"\n[CONNECT] {NEO4J_URI}")
driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
driver.verify_connectivity()
print("  Connected")

# --- Main import pass ---------------------------------------------------------

counts = {}

with driver.session() as session:
    for entry in entries:
        resource = entry.get("resource", {})
        resource_type = resource.get("resourceType")
        handler = RESOURCE_HANDLERS.get(resource_type)
        if handler is None:
            continue

        label, props, refs = handler(resource)
        write_node(session, label, props)

        for rel_type, tgt_label, tgt_id in refs:
            if tgt_id:
                write_rel(session, label, props["id"], rel_type, tgt_label, tgt_id)

        counts[label] = counts.get(label, 0) + 1

print("\n[IMPORT]")
for label, count in sorted(counts.items()):
    print(f"  {label}: {count}")

# --- Summary ------------------------------------------------------------------

print("\n[SUMMARY]")
with driver.session() as session:
    result = session.run(
        "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS count ORDER BY label"
    )
    print("  Nodes:")
    for record in result:
        print(f"    {record['label']}: {record['count']}")

    result = session.run(
        "MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS count ORDER BY rel"
    )
    print("  Relationships:")
    for record in result:
        print(f"    {record['rel']}: {record['count']}")

driver.close()
print("\nDone.")
