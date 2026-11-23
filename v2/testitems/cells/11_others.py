def notebook_definition():
    meta = {"language_info": {"name": "python"},
            "kernel_info": {"name": "synapse_pyspark"},
            "microsoft": {"language": "python", "language_group": "synapse_pyspark"}}
    if IDS.get("Lakehouse"):
        meta["dependencies"] = {"lakehouse": {"default_lakehouse": IDS["Lakehouse"],
                                              "default_lakehouse_name": NAMES["Lakehouse"],
                                              "default_lakehouse_workspace_id": WS}}
    cells = [{"cell_type": "markdown", "metadata": {}, "source": ["# Backup test notebook\n", "Reads the test Delta table."]},
             {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
              "source": [f'df = spark.read.table("{PREFIX}_sales")\n', "display(df.limit(10))"]}]
    nb = {"nbformat": 4, "nbformat_minor": 5, "cells": cells, "metadata": meta}
    return {"format": "ipynb", "parts": [{"path": "artifact.content.ipynb", "payload": b64(nb),
                                          "payloadType": "InlineBase64"}]}


def pipeline_definition():
    activities = [{"name": "Wait 5 seconds", "type": "Wait", "dependsOn": [], "typeProperties": {"waitTimeInSeconds": 5}}]
    if IDS.get("Notebook"):
        activities.append({"name": "Run test notebook", "type": "TridentNotebook",
                           "dependsOn": [{"activity": "Wait 5 seconds", "dependencyConditions": ["Succeeded"]}],
                           "policy": {"timeout": "0.01:00:00", "retry": 0},
                           "typeProperties": {"notebookId": IDS["Notebook"], "workspaceId": WS}})
    content = {"properties": {"description": "Test pipeline for the Fabric migration notebooks", "activities": activities}}
    return {"parts": [{"path": "pipeline-content.json", "payload": b64(content), "payloadType": "InlineBase64"}]}


def dataflow_definition():
    query_id = str(uuid.uuid4())
    metadata = {"formatVersion": "202502", "name": NAMES["Dataflow"], "documentLocale": "en-US", "queryGroups": [],
                "queriesMetadata": {"TestQuery": {"queryId": query_id, "queryName": "TestQuery", "loadEnabled": False}}}
    mashup = ('section Section1;\nshared TestQuery = let\n'
              '    Source = #table(type table [id = Int64.Type, name = text], {{1, "alpha"}, {2, "beta"}, {3, "gamma"}})\n'
              'in\n    Source;\n')
    return {"parts": [{"path": "queryMetadata.json", "payload": b64(metadata), "payloadType": "InlineBase64"},
                      {"path": "mashup.pq", "payload": b64(mashup), "payloadType": "InlineBase64"}]}


DEFINITIONS = {"Notebook": notebook_definition, "DataPipeline": pipeline_definition, "Dataflow": dataflow_definition}

for item_type in ["Notebook", "DataPipeline", "Dataflow", "DataAgent", "GraphModel", "Ontology"]:
    if item_type not in CREATE_TYPES:
        continue
    name = NAMES[item_type]
    make = DEFINITIONS.get(item_type)
    try:
        try:
            item_id, status = ensure_item(item_type, name, make() if make else None)
            detail = "with definition" if make and status == "Created" else ""
        except HttpError as exc:
            if not make or exc.status in (401, 403):
                raise
            item_id, status = ensure_item(item_type, name)   # the definition was rejected: create it empty
            detail = f"created empty; the definition was rejected: {exc}"
        IDS[item_type] = item_id
        step(item_type, name, item_id, status, detail)
    except HttpError as exc:
        hint = " (this item type may be in preview or turned off in the tenant settings)" \
            if exc.status == 400 or "type" in exc.code.lower() else ""
        step(item_type, name, None, "Failed", f"{exc}{hint}")
