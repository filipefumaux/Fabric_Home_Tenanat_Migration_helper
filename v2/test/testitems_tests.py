import base64, hashlib, io, json, re, sys, time, types, traceback, uuid, zlib
from urllib.parse import urlsplit, parse_qs, unquote
import pandas as pd, requests

import os
NB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "out", "V2_T_Create_Test_Items.ipynb")
WS = "aaaaaaaa-0000-4000-8000-000000000001"
FAIL = []


def check(c, m):
    if not c:
        FAIL.append(m); print("   FAIL:", m)


class Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self.headers = status, headers or {}
        self.content = b"" if body is None else json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self):
        return json.loads(self.content)


class World:
    def __init__(self, unsupported=("GraphModel",), reject_dataflow_def=False):
        self.items, self.ops, self.files, self.shortcuts = [], {}, {}, set()
        self.unsupported, self.reject_df = set(unsupported), reject_dataflow_def
        self.lh_polls, self.kql_polls = 0, 0
        self.kql_rows, self.kql_fn, self.kql_tables = 0, False, set()
        self.sql = {}           # item id -> {"tables": {name: rows}, "views": set(), "procs": set()}
        self.delta_writes, self.creates, self.deleted = [], [], []

    def request(self, method, url, params=None, json=None, data=None, headers=None, **_):
        u = urlsplit(url); params = dict(params or {})
        for k, v in parse_qs(u.query).items():
            params.setdefault(k, v[0])
        path = u.path
        if u.netloc == "onelake.dfs.fabric.microsoft.com":
            assert headers["Authorization"] == "Bearer tok-storage"
            if params.get("action") == "append":
                self.files[path] = self.files.get(path, b"") + data
            elif params.get("resource") == "file":
                self.files[path] = b""
            elif params.get("action") == "flush":
                assert int(params["position"]) == len(self.files[path])
            return Resp(201)
        if u.netloc == "kusto.example":
            assert headers["Authorization"] == "Bearer tok-kusto"
            return self.kusto(path, json)
        assert u.netloc == "api.fabric.microsoft.com" and headers["Authorization"] == "Bearer tok-pbi"
        return self.fabric(method, path[len("/v1"):], params, json)

    def fabric(self, method, p, params, body):
        if p == f"/workspaces/{WS}" and method == "GET":
            return Resp(200, {"id": WS, "displayName": "Test WS", "capacityId": "cap"})
        if p == f"/workspaces/{WS}/items" and method == "GET":
            v = [i for i in self.items if not params.get("type") or i["type"] == params["type"]]
            return Resp(200, {"value": v})
        if p == f"/workspaces/{WS}/items" and method == "POST":
            t = body["type"]
            if t in self.unsupported:
                return Resp(400, {"errorCode": "UnsupportedItemType", "message": f"{t} not enabled"})
            if t == "Dataflow" and self.reject_df and "definition" in body:
                return Resp(400, {"errorCode": "InvalidDefinition", "message": "bad mashup"})
            if t == "Notebook" and "definition" in body:
                definition = body["definition"]
                parts = definition.get("parts", [])
                if (definition.get("format") != "ipynb" or len(parts) != 1
                        or parts[0].get("path") != "artifact.content.ipynb"
                        or parts[0].get("payloadType") != "InlineBase64"):
                    return Resp(400, {"errorCode": "InvalidDefinition",
                                      "message": "ipynb requires artifact.content.ipynb as InlineBase64"})
            assert not any(i["type"] == t and i["displayName"] == body["displayName"] for i in self.items), "duplicate"
            if "definition" in body:
                for part in body["definition"]["parts"]:
                    raw = base64.b64decode(part["payload"])
                    if part["path"].endswith((".json", ".ipynb")):
                        json.loads(raw)
                self.creates.append((t, body["definition"]))
            item = {"id": str(uuid.uuid4()), "type": t, "displayName": body["displayName"]}
            self.items.append(item)
            if t == "Lakehouse":
                self.items.append({"id": str(uuid.uuid4()), "type": "SQLEndpoint", "displayName": body["displayName"]})
            if t == "Eventhouse":
                self.items.append({"id": str(uuid.uuid4()), "type": "KQLDatabase", "displayName": body["displayName"],
                                   "parent": item["id"]})
            if t in ("Lakehouse", "Warehouse", "Eventhouse"):
                op = str(uuid.uuid4()); self.ops[op] = {"n": 0, "item": item}
                return Resp(202, None, {"Location": f"https://api.fabric.microsoft.com/v1/operations/{op}"})
            return Resp(201, item)
        m = re.fullmatch(r"/operations/([^/]+)(/result)?", p)
        if m:
            op = self.ops[m.group(1)]
            if m.group(2):
                return Resp(200, op["item"])
            op["n"] += 1
            return Resp(200, {"status": "Running" if op["n"] < 2 else "Succeeded"})
        m = re.fullmatch(rf"/workspaces/{WS}/lakehouses/([^/]+)", p)
        if m:
            self.lh_polls += 1
            sql = next(i for i in self.items if i["type"] == "SQLEndpoint")
            st = "InProgress" if self.lh_polls < 2 else "Success"
            return Resp(200, {"properties": {"sqlEndpointProperties": {"id": sql["id"], "provisioningStatus": st}}})
        m = re.fullmatch(rf"/workspaces/{WS}/items/([^/]+)/shortcuts", p)
        if m and method == "POST":
            assert params.get("shortcutConflictPolicy") == "Abort"
            key = (m.group(1), body["path"], body["name"])
            if key in self.shortcuts:
                return Resp(409, {"errorCode": "EntityConflict", "message": "Shortcut already exists"})
            self.shortcuts.add(key)
            return Resp(201, body)
        if p == f"/workspaces/{WS}/kqlDatabases":
            self.kql_polls += 1
            v = [{"id": i["id"], "displayName": i["displayName"],
                  "properties": {"parentEventhouseItemId": i["parent"].upper(),
                                 **({"queryServiceUri": "https://kusto.example", "databaseName": i["displayName"]}
                                    if self.kql_polls > 1 else {})}} for i in self.items if i["type"] == "KQLDatabase"]
            return Resp(200, {"value": v})
        m = re.fullmatch(rf"/workspaces/{WS}/items/([^/]+)", p)
        if m and method == "DELETE":
            self.deleted.append(next(i for i in self.items if i["id"] == m.group(1))["type"])
            self.delete_modes = getattr(self, "delete_modes", []) + [params.get("hardDelete")]
            self.items = [i for i in self.items if i["id"] != m.group(1)]
            return Resp(200)
        raise AssertionError(f"unexpected {method} {p}")

    def kusto(self, path, body):
        csl = body["csl"]
        tbl = lambda cols, rows: Resp(200, {"Tables": [{"Columns": [{"ColumnName": c} for c in cols], "Rows": rows}]})
        if path.endswith("/query"):
            assert csl == "bkp_test_events | count"
            return tbl(["Count"], [[self.kql_rows]])
        if csl.startswith(".create-merge table bkp_test_events"):
            self.kql_tables.add("bkp_test_events"); return tbl(["x"], [])
        if csl.startswith(".set-or-append bkp_test_events <| range i from 1 to 1000"):
            self.kql_rows += 1000; return tbl(["x"], [])
        if csl.startswith(".create-or-alter function"):
            self.kql_fn = True; return tbl(["x"], [])
        raise AssertionError(csl)


class Conn:
    def __init__(self, world, item):
        self.db = world.sql.setdefault(item, {"tables": {}, "views": set(), "procs": set()})

    def query(self, q):
        m = re.fullmatch(r"EXEC\(N'(.*)'\); SELECT 1 AS done;", q, re.S)
        if m:
            s = m.group(1).replace("''", "'")
            if s.startswith("IF OBJECT_ID"):
                name = re.search(r"CREATE TABLE (\S+)", s).group(1)
                self.db["tables"].setdefault(name, 0)
            elif s.startswith("INSERT INTO"):
                name = s.split()[2]
                self.db["tables"][name] += len(re.findall(r"\(\d+, 'customer", s))
            elif s.startswith("CREATE OR ALTER VIEW"):
                self.db["views"].add(s.split()[4])
            elif s.startswith("CREATE OR ALTER PROCEDURE"):
                self.db["procs"].add(s.split()[4])
            else:
                raise AssertionError(s)
            return pd.DataFrame({"done": [1]})
        if q == "SELECT 1 AS ready":
            return pd.DataFrame({"ready": [1]})
        m = re.fullmatch(r"SELECT COUNT\(\*\) FROM sys.views WHERE name = '(\w+)'", q)
        if m:
            return pd.DataFrame({"n": [int(f"dbo.{m.group(1)}" in self.db["views"])]})
        m = re.fullmatch(r"SELECT COUNT\(\*\) FROM (\S+)", q)
        if m:
            return pd.DataFrame({"n": [self.db["tables"][m.group(1)]]})
        raise AssertionError(q)


def run(world, extra_settings="", extra_cleanup=None, seed_setup="", capture_error=False):
    nu = types.ModuleType("notebookutils")
    nu.credentials = types.SimpleNamespace(getToken=lambda a: f"tok-{a}")
    nu.runtime = types.SimpleNamespace(context={"currentWorkspaceId": WS.upper()})
    wh_ready = {"n": 0}

    def connect(item, ws, kind):
        assert ws == WS
        if kind == "Warehouse":
            wh_ready["n"] += 1
            if wh_ready["n"] < 2:
                raise ConnectionError("warehouse not ready")
        return Conn(world, item)
    nu.data = types.SimpleNamespace(connect_to_artifact=connect)
    sys.modules["notebookutils"] = nu
    dl = types.ModuleType("deltalake")
    dl.write_deltalake = (world.delta_write if hasattr(world, "delta_write") else
                         lambda uri, df, mode, storage_options: world.delta_writes.append((uri, len(df), mode, storage_options)))
    dl.CommitProperties = lambda **kw: types.SimpleNamespace(**kw)
    dl.Transaction = lambda app_id, version: types.SimpleNamespace(app_id=app_id, version=version)
    dl.WriterProperties = lambda **kw: types.SimpleNamespace(**kw)
    sys.modules["deltalake"] = dl
    requests.Session.request = lambda self, m, url, **kw: world.request(m, url, **kw)
    time.sleep = lambda s: None
    nb = json.load(open(NB, encoding="utf-8"))
    ns = {"display": lambda df: None}
    world.namespace = ns
    for i, c in enumerate([c for c in nb["cells"] if c["cell_type"] == "code"]):
        src = "".join(c["source"])
        if extra_cleanup and "DELETE_TEST_ITEMS = False" in src:
            src = src.replace("DELETE_TEST_ITEMS = False", "DELETE_TEST_ITEMS = True")
        try:
            exec(compile(src, f"cell{i}", "exec"), ns)
        except Exception as exc:
            if not capture_error:
                raise
            ns["_error"] = exc
            break
        if i == 0:
            exec(extra_settings, ns)
        if "def seed_large_lakehouse(" in src and seed_setup:
            exec(seed_setup, ns)
    return ns


def st(ns, t):
    return [s["Status"] for s in ns["STEPS"] if s["Type"] == t]


def t_fresh_and_rerun():
    w = World()
    ns = run(w)
    types_ = sorted(i["type"] for i in w.items)
    check(types_ == sorted(["Lakehouse", "SQLEndpoint", "Warehouse", "Eventhouse", "KQLDatabase", "Notebook",
                            "DataPipeline", "Dataflow", "DataAgent", "Ontology"]), types_)
    check(st(ns, "GraphModel") == ["Failed"] and "preview" in ns["STEPS"][-1]["Detail"] or
          any("preview" in s["Detail"] for s in ns["STEPS"] if s["Type"] == "GraphModel"), "graphmodel failure hint")
    lh = ns["IDS"]["Lakehouse"]
    check(w.files[f"/{WS}/{lh}/Files/raw/sample.csv"].count(b"\n") == 101, "csv")
    check(f"/{WS}/{lh}/Files/raw/nested/readme.txt" in w.files, "nested file")
    check(w.delta_writes[0][0] == f"abfss://{WS}@onelake.dfs.fabric.microsoft.com/{lh}/Tables/bkp_test_sales"
          and w.delta_writes[0][1] == 1000, w.delta_writes)
    check(w.delta_writes[0][3] == {"bearer_token": "tok-storage", "use_fabric_endpoint": "true"}, "delta auth")
    check(len(w.shortcuts) == 1, "shortcut")
    check(st(ns, "SQLEndpoint") == ["OK"], f"sql endpoint {st(ns, 'SQLEndpoint')}")
    wh = w.sql[ns["IDS"]["Warehouse"]]
    check(wh["tables"]["dbo.bkp_test_orders"] == 500 and len(wh["views"]) == 1 and len(wh["procs"]) == 1, wh)
    check(st(ns, "Warehouse") == ["Created", "OK"], st(ns, "Warehouse"))
    check(w.kql_rows == 1000 and w.kql_fn and st(ns, "KQLDatabase") == ["OK"], "kql")
    defs = dict(w.creates)
    pipe = json.loads(base64.b64decode(defs["DataPipeline"]["parts"][0]["payload"]))
    act = pipe["properties"]["activities"][1]
    check(act["typeProperties"]["notebookId"] == ns["IDS"]["Notebook"], "pipeline -> notebook")
    nbdef = json.loads(base64.b64decode(defs["Notebook"]["parts"][0]["payload"]))
    check(nbdef["metadata"]["dependencies"]["lakehouse"]["default_lakehouse"] == lh, "notebook -> lakehouse")
    check(defs["Notebook"]["format"] == "ipynb", "ipynb format")
    check({p["path"] for p in defs["Dataflow"]["parts"]} == {"queryMetadata.json", "mashup.pq"}, "dataflow parts")
    # rerun: nothing duplicated, no rows added
    n_items = len(w.items)
    ns2 = run(w)
    check(len(w.items) == n_items, "rerun created items")
    check(w.kql_rows == 1000, "kql rows doubled")
    check(w.sql[ns2["IDS"]["Warehouse"]]["tables"]["dbo.bkp_test_orders"] == 500, "warehouse rows doubled")
    check(st(ns2, "Notebook") == ["Exists"] and "Exists" in st(ns2, "Lakehouse"), "exists statuses")
    # cleanup
    w.items.append({"id": str(uuid.uuid4()), "type": "Notebook", "displayName": "keep_me"})
    w.items.append({"id": str(uuid.uuid4()), "type": "Warehouse", "displayName": "StagingWarehouseForDataflows_20261117090000"})
    w.items.append({"id": str(uuid.uuid4()), "type": "Notebook", "displayName": "StagingNotebook_keep"})
    run(w, extra_cleanup=True)
    check(set(w.delete_modes) == {"true"}, f"hard delete {w.delete_modes}")
    check(not any(i["displayName"].startswith("StagingWarehouseForDataflows") for i in w.items), "staging deleted")
    w.items = [i for i in w.items if i["displayName"] != "StagingNotebook_keep"]
    left = [(i["type"], i["displayName"]) for i in w.items]
    check(left == [("Notebook", "keep_me")] or set(left) <= {("Notebook", "keep_me"), ("SQLEndpoint", "bkp_test_lakehouse"),
                                                              ("KQLDatabase", "bkp_test_eventhouse")}, left)
    check(w.deleted[0] == "DataPipeline" and "SQLEndpoint" not in w.deleted, w.deleted)


def t_dataflow_fallback():
    w = World(unsupported=(), reject_dataflow_def=True)
    ns = run(w)
    s = [x for x in ns["STEPS"] if x["Type"] == "Dataflow"][0]
    check(s["Status"] == "Created" and "rejected" in s["Detail"], s)
    check(st(ns, "GraphModel") == ["Created"], "graphmodel ok when supported")
    check(not [x for x in ns["STEPS"] if x["Status"] == "Failed"], [x for x in ns["STEPS"] if x["Status"] == "Failed"])


class StressWorld(World):
    """Local only: Delta publication, hidden WH files, COPY and tagged pull ingestion."""
    def __init__(self):
        super().__init__(unsupported=())
        self.file_props, self.sources, self.stress_sql, self.stress_kql = {}, {}, {}, {}
        self.source_frames, self.copies, self.ingests, self.transactions = [], [], [], set()
        self.lh_rate, self.wh_rate, self.kql_rate = 1024, 768, 1536  # mock physical bytes/row, NOT payload lengths
        self.zero_delta, self.zero_kql, self.fail_delta_once = False, False, False
        self.fail_copy_once, self.fail_kql_once, self.sql_schema_collision = False, False, False
        self.ingest_requests = 0
        self.extent_kind = "ColumnStore"
        self.unowned_extent_tag = False
        self.list_pages, self.metric_reads = 0, 0

    def request(self, method, url, params=None, json=None, data=None, headers=None, **kw):
        u = urlsplit(url)
        if u.netloc != "onelake.dfs.fabric.microsoft.com":
            return super().request(method, url, params=params, json=json, data=data, headers=headers, **kw)
        assert headers["Authorization"] == "Bearer tok-storage"
        p = unquote(u.path); params = dict(params or {})
        if params.get("resource") == "filesystem":
            assert method == "GET"
            self.list_pages += 1
            directory = "/" + WS + "/" + params["directory"].strip("/")
            # Warehouse Parquet files deliberately never appear in a directory listing.
            paths = [k for k in sorted(self.files) if k.startswith(directory + "/")
                     and "/" not in k[len(directory) + 1:]
                     and not ("/Tables/dbo/" in k and k.endswith(".parquet") and "/_delta_log/" not in k)]
            offset = int(params.get("continuation") or 0)
            page = paths[offset:offset + 7]
            hdr = {"x-ms-continuation": str(offset + 7)} if len(paths) > offset + 7 else {}
            return Resp(200, {"paths": [{"name": k[len("/" + WS + "/"):], "isDirectory": False,
                                        "contentLength": str(len(self.files[k]))} for k in page]}, hdr)
        if method in ("GET", "HEAD"):
            if p not in self.files:
                return Resp(404, {"error": {"code": "PathNotFound", "message": p}})
            hdr = {"Content-Length": str(len(self.files[p])), "ETag": '"etag"',
                   "x-ms-properties": self.file_props.get(p, "")}
            r = Resp(200, None, hdr)
            r.content = self.files[p] if method == "GET" else b""
            r.text = r.content.decode("utf-8", errors="replace")
            return r
        if params.get("resource") == "file":
            if headers.get("If-None-Match") == "*" and p in self.files:
                return Resp(412, {"error": {"code": "ConditionNotMet", "message": "already exists"}})
            if "/_v2t_stress/" in p:
                assert headers.get("If-None-Match") == "*", "stress files must be exclusive creates"
            self.files[p] = b""
            self.file_props[p] = headers.get("x-ms-properties", "")
            return Resp(201)
        if params.get("action") == "append":
            if any(k.lower() in ("if-match", "if-none-match", "if-modified-since", "if-unmodified-since")
                   for k in headers):
                return Resp(400, {"error": {"code": "InvalidInput",
                                           "message": "Conditional headers are invalid for Append Data"}})
            assert int(params["position"]) == len(self.files[p]), "non-append write"
            self.files[p] += data
            return Resp(202)
        if params.get("action") == "flush":
            if "/_v2t_stress/" in p:
                assert headers.get("If-Match") == '"etag"', "owned source flush must preserve the read ETag condition"
            assert int(params["position"]) == len(self.files[p])
            if self.files[p].startswith(b"FAKEPARQUET\n"):
                frame = pd.read_json(io.StringIO(self.files[p].split(b"\n", 1)[1].decode()), orient="split")
                self.sources[p] = frame
            return Resp(200)
        raise AssertionError((method, p, params))

    def fabric(self, method, p, params, body):
        r = super().fabric(method, p, params, body)
        if method == "POST" and p == f"/workspaces/{WS}/items":
            item = next(i for i in self.items if i["type"] == body["type"] and i["displayName"] == body["displayName"])
            item["description"] = body.get("description", "")
        return r

    def publish_delta(self, item, table_path, frame, size, description=None, append=True):
        root = f"/{WS}/{item}/{table_path}"
        old = [p for p in self.files if re.fullmatch(re.escape(root) + r"/_delta_log/\d{20}\.json", p)]
        version = len(old)
        actions = []
        if not old:
            fields = [{"name": c, "type": "long" if pd.api.types.is_integer_dtype(frame[c]) else "string",
                       "nullable": True, "metadata": {}} for c in frame.columns]
            actions += [{"protocol": {"minReaderVersion": 1, "minWriterVersion": 2}},
                        {"metaData": {"id": str(uuid.uuid4()), "description": description,
                                      "schemaString": json.dumps({"type": "struct", "fields": fields}),
                                      "partitionColumns": [], "configuration": {}}}]
        if not append:
            for p in old:
                for line in self.files[p].decode().splitlines():
                    a = json.loads(line).get("add")
                    if a:
                        actions.append({"remove": {"path": a["path"]}})
        filename = f"part-{version:05d}.parquet"
        stats = {"numRecords": len(frame)}
        if "seed_batch" in frame:
            keys = ["seed_batch", "seed_row", "seed_owner"]
            stats.update(minValues={c: frame[c].min().item() if c != "seed_owner" else frame[c].min() for c in keys},
                         maxValues={c: frame[c].max().item() if c != "seed_owner" else frame[c].max() for c in keys},
                         nullCount={c: 0 for c in frame.columns})
        actions.append({"add": {"path": filename, "size": size, "stats": json.dumps(stats), "partitionValues": {}}})
        self.files[f"{root}/_delta_log/{version:020d}.json"] = b"\n".join(json.dumps(a).encode() for a in actions)
        self.files[f"{root}/{filename}"] = b"not listed Warehouse data"  # not used for the byte metric
        self.sources[f"{root}/{filename}"] = frame.copy()

    def delta_write(self, uri, frame, mode, storage_options, **kw):
        self.delta_writes.append((uri, len(frame), mode, storage_options))
        u = urlsplit(uri); item, table_path = u.path.strip("/").split("/", 1)
        assert "SQLEndpoint" not in uri
        if "seed_owner" in frame:
            assert mode in ("error", "append") and kw.get("description"), "owned stress data must never overwrite"
            props = kw["commit_properties"]; txn = props.app_transactions[0]
            if mode == "append" and props.max_commit_retries == 0:
                raise RuntimeError("Failed to commit transaction: 0")
            key = (txn.app_id, txn.version)
            if key in self.transactions:
                return
            self.transactions.add(key)
            self.source_frames.append(frame.copy())
        self.publish_delta(item, table_path, frame, 0 if self.zero_delta else len(frame) * self.lh_rate,
                           kw.get("description"), mode == "append")
        if "seed_owner" in frame and self.fail_delta_once:
            self.fail_delta_once = False
            raise RuntimeError("lost Delta acknowledgement " + "x" * 650 + " END_FULL_ERROR")

    def kusto(self, path, body):
        csl = body["csl"]
        response = lambda rows: Resp(200, {"Tables": [{"Columns": [{"ColumnName": c} for c in (list(rows[0]) if rows else [])],
                                                     "Rows": [list(r.values()) for r in rows]}]})
        m = re.fullmatch(r"\.show tables \| where TableName == '(\w+)'", csl)
        if m:
            entry = self.stress_kql.get(m[1])
            return response([{"TableName": m[1], "DocString": entry["owner"]}] if entry else [])
        m = re.fullmatch(r"\.create table (\w+) \((.*?)\) with \(docstring='([^']+)'\)", csl)
        if m:
            assert m[1] not in self.stress_kql, "must not merge/alter a colliding table"
            self.stress_kql[m[1]] = {"owner": m[3], "frames": [], "tags": set()}
            return response([{"TableName": m[1]}])
        m = re.fullmatch(r"\.show table (\w+) schema as json", csl)
        if m:
            cols = [{"Name": c, "CslType": t} for c, t in
                    [("seed_owner", "string"), ("seed_batch", "long"), ("seed_row", "long"), ("payload", "string")]]
            return response([{"Schema": json.dumps({"OrderedColumns": cols})}])
        m = re.fullmatch(r"\.show table (\w+) extents", csl)
        if m:
            self.metric_reads += 1
            if m[1].endswith("_events"):
                return response([{"ExtentId": "small", "RowCount": self.kql_rows,
                                  "CompressedSize": 0 if self.zero_kql else self.kql_rows * 24,
                                  "OriginalSize": 10**12, "ExtentSize": 10**11, "Kind": self.extent_kind, "Tags": "[]"}])
            entry = self.stress_kql[m[1]]
            return response([{"ExtentId": str(n), "RowCount": len(f),
                              "CompressedSize": 0 if self.zero_kql else len(f) * self.kql_rate,
                              "OriginalSize": 10**12, "ExtentSize": 10**11, "Kind": self.extent_kind,
                              "Tags": "\r\n".join(sorted(entry["tags"]))} for n, f in enumerate(entry["frames"])])
        m = re.match(r"\.ingest into table (\w+) \((.*?)\) with \((.*)\)$", csl)
        if m:
            table, locators, options = m.groups()
            self.ingest_requests += 1
            assert "format='parquet'" in options and "ingestionMapping=" in options
            tags = json.loads(re.search(r"tags='([^']+)'", options)[1])
            guard = json.loads(re.search(r"ingestIfNotExists='([^']+)'", options)[1])
            assert tags == ["ingest-by:" + guard[0]]
            entry = self.stress_kql[table]
            if tags[0] not in entry["tags"]:
                frames = []
                for locator in re.findall(r"h'([^']+)'", locators):
                    assert locator.endswith(";impersonate") and "*" not in locator
                    u = urlsplit(locator.removesuffix(";impersonate"))
                    frames.append(self.sources[f"/{u.netloc.split('@')[0]}{unquote(u.path)}"])
                entry["frames"] += frames
                entry["tags"].add(tags[0]); self.ingests.append((table, locators, guard[0]))
                if self.fail_kql_once:
                    self.fail_kql_once = False
                    return Resp(503, {"error": {"code": "LostAcknowledgement", "message": "ingest committed; reply lost"}})
            else:
                return Resp(409, {"error": {"code": "DataAlreadyExists", "message": "ingest-by tag already exists"}})
            return response([{"HasErrors": False, "ExtentId": "extent"}])
        if csl.endswith("| count") and csl.split()[0] in self.stress_kql:
            return response([{"Count": sum(len(f) for f in self.stress_kql[csl.split()[0]]["frames"])}])
        if "extend ExtentTags=extent_tags()" in csl:
            tags = sorted(self.stress_kql[csl.split()[0]]["tags"])
            if self.unowned_extent_tag:
                tags.append("foreign-extent")
            return response([{"Tags": json.dumps(tags)}])
        if "| summarize" in csl and csl.split()[0] in self.stress_kql:
            frames = self.stress_kql[csl.split()[0]]["frames"]
            return response(stress_groups(pd.concat(frames, ignore_index=True)) if frames else [])
        if "payload_hash=hash_sha256(payload)" in csl:
            frames = self.stress_kql[csl.split()[0]]["frames"]
            return response(stress_hashes(pd.concat(frames, ignore_index=True)))
        return super().kusto(path, body)


def stress_groups(frame):
    rows = []
    for batch, g in frame.groupby("seed_batch"):
        rows.append({"seed_batch": int(batch), "rows": len(g), "distinct_rows": int(g.seed_row.nunique()),
                     "first_row": int(g.seed_row.min()), "last_row": int(g.seed_row.max()),
                     "owner_min": g.seed_owner.min(), "owner_max": g.seed_owner.max(),
                     "bad_payload": int(g.payload.isna().sum())})
    return rows


def stress_hashes(frame):
    wanted = {int(frame.seed_row.min()), int(frame.seed_row.max())}
    return [{"seed_row": int(r.seed_row), "payload_hash": hashlib.sha256(r.payload.encode()).hexdigest()}
            for r in frame.itertuples() if int(r.seed_row) in wanted]


_original_query = Conn.query


def stress_query(self, query):
    w = getattr(self, "world", None)
    if not isinstance(w, StressWorld):
        return _original_query(self, query)
    m = re.fullmatch(r"EXEC\(N'(.*)'\); SELECT 1 AS done;", query, re.S)
    statement = m[1].replace("''", "'") if m else query
    table_match = re.search(r"dbo\.(\w+_stress)\b", statement)
    table = "dbo." + table_match[1] if table_match else None
    if "INFORMATION_SCHEMA.COLUMNS" in statement:
        name = re.search(r"TABLE_NAME = '(\w+)'", statement)[1]
        exists = "dbo." + name in w.stress_sql
        return pd.DataFrame([{"COLUMN_NAME": c, "DATA_TYPE": t,
                              "CHARACTER_MAXIMUM_LENGTH": 4096 if c == "payload" and w.sql_schema_collision else n}
                             for c, t, n in [("seed_owner", "varchar", 64), ("seed_batch", "bigint", None),
                                             ("seed_row", "bigint", None), ("payload", "varchar", 2048)]]
                            if exists else [], columns=["COLUMN_NAME", "DATA_TYPE", "CHARACTER_MAXIMUM_LENGTH"])
    if table and statement.startswith("CREATE TABLE"):
        assert table not in w.stress_sql
        w.stress_sql[table] = {"frames": []}
        return pd.DataFrame({"done": [1]})
    if table and statement.startswith("COPY INTO"):
        assert "*" not in statement and "CREDENTIAL" not in statement, "no wildcard or new credential"
        files = re.findall(r"'(https://[^']+)'", statement)
        assert files
        frames = [w.sources[unquote(urlsplit(url).path)] for url in files]
        w.stress_sql[table]["frames"] += frames
        w.copies.append((table, files))
        frame = pd.concat(frames, ignore_index=True)
        w.publish_delta(self.item, "Tables/" + table.replace(".", "/"), frame,
                        0 if w.zero_delta else len(frame) * w.wh_rate)
        if w.fail_copy_once:
            w.fail_copy_once = False
            raise RuntimeError("COPY committed but acknowledgement lost; rerun must reconcile rows first")
        return pd.DataFrame({"done": [1]})
    if table and "GROUP BY seed_batch" in statement:
        frames = w.stress_sql[table]["frames"]
        return pd.DataFrame(stress_groups(pd.concat(frames, ignore_index=True)) if frames else [])
    if table and "HASHBYTES" in statement:
        return pd.DataFrame(stress_hashes(pd.concat(w.stress_sql[table]["frames"], ignore_index=True)))
    result = _original_query(self, query)
    if statement.startswith("INSERT INTO dbo.") and "_orders " in statement:
        name = statement.split()[2]
        frame = pd.DataFrame({"OrderId": range(500)})
        w.publish_delta(self.item, "Tables/" + name.replace(".", "/"), frame, len(frame) * 32)
    return result


_original_conn_init = Conn.__init__


def stress_conn_init(self, world, item):
    self.world, self.item = world, item
    _original_conn_init(self, world, item)


Conn.__init__, Conn.query = stress_conn_init, stress_query
STORAGE_TYPES = "CREATE_TYPES = ['Lakehouse', 'Warehouse', 'Eventhouse']\n"
HELPERS_ONLY = "CREATE_TYPES = ['Notebook']\n"
LARGE = STORAGE_TYPES + "LARGE_DATA_MIB = 1\nMEASURE_DATA_SIZE = True\n"
FAKE_PARQUET = "_seed_encode_parquet = lambda frame: b'FAKEPARQUET\\n' + frame.to_json(orient='split').encode()\n"


def assert_raises(fn, text):
    try:
        fn()
    except Exception as exc:
        check(text.lower() in str(exc).lower(), f"expected {text!r}, got {exc}")
        return
    check(False, f"expected failure containing {text!r}")


def t_seed_profile_and_entropy():
    ns = run(StressWorld(), HELPERS_ONLY)
    check(ns.get("LARGE_DATA_MIB") == 0 and ns.get("MEASURE_DATA_SIZE") is False, "legacy defaults")
    check(ns.get("SEED_METRICS") == {}, "legacy mode must not pretend sizes were measured")
    payload = ns["seed_payload"]
    values = [payload("a" * 64, n) for n in range(128)]
    raw = "".join(values).encode()
    check(len(set(values)) == 128 and all(len(v) == 2048 for v in values), "independent full-entropy payloads")
    check(len(zlib.compress(raw)) > len(raw) * .70, "compressible filler is not stress data")
    check(payload("a" * 64, 0) == values[0] and payload("b" * 64, 0) != values[0], "deterministic scope isolation")
    for bad in ("-1", "True", "1.5"):
        w = StressWorld()
        ns = run(w, f"LARGE_DATA_MIB = {bad}\n", capture_error=True)
        check("_error" in ns and not w.items, f"invalid LARGE_DATA_MIB {bad} must stop before creation")
    ns = run(StressWorld(), "LARGE_DATA_MIB = 1\nCREATE_TYPES = ['Warehouse']\n", capture_error=True)
    check("_error" in ns, "large bulk loads require this group's Lakehouse")


def t_seed_delta_active_bytes_and_gaps():
    w = StressWorld(); ns = run(w, HELPERS_ONLY)
    root = f"/{WS}/item/Tables/dbo/owned/_delta_log"
    put = lambda n, actions: w.files.__setitem__(f"{root}/{n:020d}.json",
                                                b"\n".join(json.dumps(a).encode() for a in actions))
    put(0, [{"add": {"path": "old.parquet", "size": 999999, "stats": '{"numRecords":9}'}}])
    put(1, [{"remove": {"path": "old.parquet"}},
            {"add": {"path": "new.parquet", "size": 4312, "stats": '{"numRecords":3}'}}])
    snap = ns["seed_delta_snapshot"]("item", "Tables/dbo/owned")
    check(snap["storedBytes"] == 4312 and snap["rows"] == 3, "active replay, not all add sizes or file listing")
    check(list(snap["active"]) == ["new.parquet"], "tombstones applied")
    put(3, [{"add": {"path": "unpublished.parquet", "size": 5, "stats": '{"numRecords":1}'}}])
    assert_raises(lambda: ns["seed_delta_snapshot"]("item", "Tables/dbo/owned"), "gap")
    del w.files[f"{root}/{3:020d}.json"]
    put(2, [{"add": {"path": "../outside.parquet", "size": 5, "stats": '{"numRecords":1}'}}])
    assert_raises(lambda: ns["seed_delta_snapshot"]("item", "Tables/dbo/owned"), "path")


def t_seed_large_bulk_metrics_reuse_and_resume():
    w = StressWorld()
    ns = run(w, LARGE, seed_setup=FAKE_PARQUET)
    check("_error" not in ns, ns.get("_error"))
    metrics = ns.get("SEED_METRICS", {})
    check(set(metrics) == {"Lakehouse", "Warehouse", "Eventhouse"}, metrics)
    for family, m in metrics.items():
        check(m["status"] == "OK" and type(m["rows"]) is int and m["rows"] > 0, (family, m))
        check(type(m["storedBytes"]) is int and .95 * 1024**2 <= m["storedBytes"] <= 1.10 * 1024**2, (family, m))
        check(m["targetBytes"] == 1024**2 and m["table"], (family, m))
        allowed = {ns["IDS"][family]}
        if family == "Eventhouse":
            allowed.add(ns["IDS"]["KQLDatabase"])
        check(m["itemId"] in allowed, (family, m))
        check(m["sizeMetric"] == ("compressed_extent_bytes" if family == "Eventhouse" else "active_parquet_bytes"),
              (family, m))
    check(w.copies and w.ingests, "large rows must use bulk source paths")
    lh = ns["IDS"]["Lakehouse"]
    first_copy = {unquote(urlsplit(u).path) for _, files in w.copies for u in files}
    first_ingest = {f"/{urlsplit(u.removesuffix(';impersonate')).netloc.split('@')[0]}"
                    f"{unquote(urlsplit(u.removesuffix(';impersonate')).path)}"
                    for _, locators, _ in w.ingests for u in re.findall(r"h'([^']+)'", locators)}
    check(first_copy & first_ingest and all(f"/{lh}/" in p for p in first_copy | first_ingest), "source reused across families")
    check(max(len(f) for f in w.source_frames) <= ns["_SEED_MAX_ROWS"], "bounded generation")
    check(w.sql[ns["IDS"]["Warehouse"]]["tables"]["dbo.bkp_test_orders"] == 500 and w.kql_rows == 1000,
          "large profile must keep original small fixtures")
    before = len(w.source_frames), len(w.copies), len(w.ingests), len(w.transactions)
    ns2 = run(w, LARGE, seed_setup=FAKE_PARQUET)
    check((len(w.source_frames), len(w.copies), len(w.ingests), len(w.transactions)) == before, "rerun duplicates")
    check(ns2["SEED_METRICS"] == metrics, "rerun measures the same persisted data")
    check(w.list_pages > 7, "paged metadata used, not hidden Warehouse data files")


def t_seed_small_measurement_is_real():
    w = StressWorld()
    ns = run(w, STORAGE_TYPES + "MEASURE_DATA_SIZE = True\n")
    metrics = ns.get("SEED_METRICS", {})
    check(set(metrics) == {"Lakehouse", "Warehouse", "Eventhouse"}, "small metrics required by parent")
    for family, expected in (("Lakehouse", 1000), ("Warehouse", 500), ("Eventhouse", 1000)):
        m = metrics[family]
        check(m["rows"] == expected and m["storedBytes"] > 0 and m["targetBytes"] == 0 and m["status"] == "OK", m)
        check(m["storedBytes"] < .1 * 1024**3, "small stored bytes below parent's 10%-of-1-GiB threshold")
    check(not w.copies and not w.ingests and not w.source_frames, "measurement must not generate stress data")


def t_seed_fail_closed_sizes_and_lost_ack():
    for family in ("delta", "kql"):
        w = StressWorld(); setattr(w, "zero_" + family, True)
        ns = run(w, STORAGE_TYPES + "MEASURE_DATA_SIZE = True\n",
                 seed_setup="_SEED_PUBLISH_ATTEMPTS = 3\n", capture_error=True)
        check("_error" in ns, f"populated {family} with unpublished size must fail")
        check(any(m["status"] != "OK" for m in ns.get("SEED_METRICS", {}).values()), "no false OK on zero bytes")
    w = StressWorld(); w.fail_delta_once = True
    ns = run(w, LARGE, seed_setup=FAKE_PARQUET, capture_error=True)
    check("_error" in ns, "lost acknowledgement must surface")
    check("END_FULL_ERROR" in str(ns.get("_error")) or
          any("END_FULL_ERROR" in s["Detail"] for s in ns["STEPS"]), "full error text")
    committed = len(w.source_frames)
    ns2 = run(w, LARGE, seed_setup=FAKE_PARQUET)
    check(ns2["SEED_METRICS"]["Lakehouse"]["status"] == "OK" and len(w.transactions) == len(w.source_frames),
          "resume committed batch instead of duplicating it")
    check(committed > 0, "test must interrupt after a real fake commit")
    # A metric over the upper bound is not fixed by deleting data, hiding tables, or claiming OK.
    ns = run(StressWorld(), HELPERS_ONLY)
    assert_raises(lambda: ns["seed_size_status"](1200, 1000, 2), "size")
    assert_raises(lambda: ns["seed_size_status"](0, 0, 1000), "size")


def t_seed_refuses_ownership_and_schema_collisions():
    w = StressWorld(); ns = run(w, LARGE, seed_setup=FAKE_PARQUET)
    root = f"/{WS}/{ns['IDS']['Lakehouse']}/Tables/bkp_test_stress/_delta_log"
    log = f"{root}/{0:020d}.json"
    entries = [json.loads(s) for s in w.files[log].decode().splitlines()]
    for a in entries:
        if "metaData" in a:
            a["metaData"]["description"] = "user data"
    w.files[log] = b"\n".join(json.dumps(a).encode() for a in entries)
    before = len(w.source_frames), len(w.copies), len(w.ingests)
    failed = run(w, LARGE, seed_setup=FAKE_PARQUET, capture_error=True)
    check("_error" in failed and failed["SEED_METRICS"]["Lakehouse"]["status"] != "OK", "foreign table refused")
    check(before == (len(w.source_frames), len(w.copies), len(w.ingests)), "collision must not append")
    # Matching names alone cannot adopt a table without the owned plan marker.
    w = StressWorld(); ns = run(w, STORAGE_TYPES + "MEASURE_DATA_SIZE = True\n")
    w.stress_sql["dbo.bkp_test_stress"] = {"frames": []}
    failed = run(w, LARGE, seed_setup=FAKE_PARQUET, capture_error=True)
    check("_error" in failed and failed["SEED_METRICS"]["Warehouse"]["status"] != "OK", "unowned empty SQL table refused")


def t_seed_checkpoint_replay_and_newer_checkpoint_invalidates_cache():
    w = StressWorld(); ns = run(w, HELPERS_ONLY)
    root = f"/{WS}/item/Tables/owned/_delta_log"
    checkpoint = lambda size: [{"add": {"path": "active.parquet", "size": size, "stats": '{"numRecords":7}'}}]
    ns["_seed_checkpoint_rows"] = lambda raw: iter(checkpoint(int(raw)))
    w.files[f"{root}/_last_checkpoint"] = b'{"version":5}'
    w.files[f"{root}/{5:020d}.checkpoint.parquet"] = b"700"
    first = ns["seed_delta_snapshot"]("item", "Tables/owned")
    check(first["storedBytes"] == 700 and first["rows"] == 7, "checkpoint active bytes")
    w.files[f"{root}/_last_checkpoint"] = b'{"version":7}'
    w.files[f"{root}/{7:020d}.checkpoint.parquet"] = b"600"
    latest = ns["seed_delta_snapshot"]("item", "Tables/owned")
    check(latest["storedBytes"] == 600 and latest["version"] == 7, "new checkpoint must invalidate stale metric cache")
    w.files[f"{root}/{8:020d}.json"] = b""
    assert_raises(lambda: ns["seed_delta_snapshot"]("item", "Tables/owned"), "commit")


def t_seed_kql_extent_kind_is_not_a_columnstore_flag():
    w = StressWorld(); w.extent_kind = "EngineV3"
    ns = run(w, STORAGE_TYPES + "MEASURE_DATA_SIZE = True\n",
             seed_setup="_SEED_PUBLISH_ATTEMPTS = 2\n", capture_error=True)
    check("_error" not in ns and ns["SEED_METRICS"]["Eventhouse"]["status"] == "OK",
          "Kind identifies the storage engine, not a documented literal ColumnStore flag")


def t_seed_owned_upload_recovery_and_runtime_requirement():
    w = StressWorld(); ns = run(w, HELPERS_ONLY)
    owner, path, data = "a" * 64, "Files/_v2t_stress/probe/source.bin", b"high entropy source bytes"
    put = ns["_seed_put_once"]
    put("lh", path, data, owner)
    key = f"/{WS}/lh/{path}"
    w.files[key] = data[:9]  # interrupt a committed, owned prefix upload
    put("lh", path, data, owner)
    check(w.files[key] == data, "resume owned file by verified prefix append")
    w.files[key] = b"not our prefix"
    assert_raises(lambda: put("lh", path, data, owner), "collision")
    plan_path, plan = "Files/_v2t_stress/probe/plan.json", {"rows": 4, "seed_owner": owner}
    encoded = ns["_seed_json_bytes"](plan)
    put("lh", plan_path, encoded, owner, intent=True)
    w.files[f"/{WS}/lh/{plan_path}"] = encoded[:5]
    check(ns["_seed_get_plan"]("lh", plan_path, owner) == plan, "interrupted intent repaired from owned metadata")
    del sys.modules["deltalake"].Transaction
    assert_raises(ns["_seed_delta_runtime"], "approved compatible")


def t_seed_bulk_lost_acknowledgements_are_not_duplicates():
    for kind in ("copy", "kql"):
        w = StressWorld(); setattr(w, "fail_" + kind + "_once", True)
        first = run(w, LARGE, seed_setup=FAKE_PARQUET, capture_error=True)
        check("_error" in first, f"ambiguous {kind} acknowledgement must surface, not pretend success")
        if kind == "kql":
            check(w.ingest_requests > len(w.ingests), "HTTP retry must actually exercise ingestIfNotExists")
        resumed = run(w, LARGE, seed_setup=FAKE_PARQUET)
        check(all(m["status"] == "OK" for m in resumed["SEED_METRICS"].values()), "resume after ambiguous bulk commit")
        sql_rows = pd.concat(w.stress_sql["dbo.bkp_test_stress"]["frames"], ignore_index=True)
        kql_rows = pd.concat(w.stress_kql["bkp_test_stress"]["frames"], ignore_index=True)
        check(sql_rows.seed_row.nunique() == len(sql_rows) and kql_rows.seed_row.nunique() == len(kql_rows),
              "ambiguous bulk acknowledgements duplicated synthetic ranges")


def t_seed_schema_collision_batch_limit_and_full_service_errors():
    w = StressWorld(); ns = run(w, LARGE, seed_setup=FAKE_PARQUET)
    before = len(w.copies)
    w.sql_schema_collision = True
    failed = run(w, LARGE, seed_setup=FAKE_PARQUET, capture_error=True)
    check("_error" in failed and failed["SEED_METRICS"]["Warehouse"]["status"] != "OK" and len(w.copies) == before,
          "owned marker must not permit a changed SQL schema")
    w = StressWorld()
    failed = run(w, LARGE, seed_setup=FAKE_PARQUET + "_SEED_MAX_BATCHES = 2\n", capture_error=True)
    check("_error" in failed and len(w.source_frames) <= 2 and
          failed["SEED_METRICS"]["Lakehouse"]["status"] != "OK", "bounded failure below target")
    ns = run(StressWorld(), HELPERS_ONLY)
    message = "Kusto partial query failure " + "x" * 650 + " END_FULL_SERVICE_ERROR"
    ns["http"] = lambda *a, **k: Resp(200, {"Exceptions": [message], "Tables": []})
    assert_raises(lambda: ns["kusto"]("https://kusto.example", "db", "T | count", "query"), "END_FULL_SERVICE_ERROR")
    conn = types.SimpleNamespace(query=lambda q: pd.DataFrame({"done": [1]}),
                                 commit=lambda: (_ for _ in ()).throw(RuntimeError("commit was not acknowledged")))
    assert_raises(lambda: ns["_seed_sql_run"](conn, "COPY INTO T FROM 'owned'"), "not acknowledged")
    calls = []
    def too_large(method, *args, **kw):
        calls.append(method)
        return Resp(200, headers={"Content-Length": str(65 * 1024**2)})
    ns["http"] = too_large
    assert_raises(lambda: ns["_seed_read"]("item", "Files/foreign.bin"), "64 MiB")
    check(calls == ["HEAD"], "oversized collision must fail before downloading data")


def t_seed_isolated_runtime_integrity_and_preloaded_guard():
    w = StressWorld(); ns = run(w, HELPERS_ONLY)
    original_get = requests.get
    data, calls = b"pinned wheel bytes", []

    class WheelResponse:
        def raise_for_status(self):
            pass

        def iter_content(self, size):
            yield data

        def close(self):
            calls.append("closed")

    def public_get(url, **kwargs):
        check("headers" not in kwargs and "auth" not in kwargs, "public package request has no notebook credentials")
        return WheelResponse()

    try:
        requests.get = public_get
        fetch = ns["_seed_download_wheel"]
        check(fetch("https://files.pythonhosted.org/pinned.whl", hashlib.sha256(data).hexdigest()) == data,
              "only matching pinned bytes can reach installation")
        assert_raises(lambda: fetch("https://files.pythonhosted.org/pinned.whl", "0" * 64), "SHA-256 mismatch")
        check(calls == ["closed", "closed"], "package responses close on success and hash rejection")
        loaded = sys.modules["deltalake"]
        assert_raises(ns["seed_prepare_runtime"], "already imported")
        check(sys.modules["deltalake"] is loaded and ns["_SEED_RUNTIME_TEMP"] is None,
              "never replace a loaded native package or change a shared environment")
    finally:
        requests.get = original_get


def t_seed_typed_extent_tags_still_refuse_foreign_data():
    w = StressWorld(); w.unowned_extent_tag = True
    ns = run(w, LARGE, seed_setup=FAKE_PARQUET, capture_error=True)
    check("_error" in ns and "Untracked KQL extent tags" in str(ns["_error"]),
          "typed extent-tag response does not waive foreign-tag protection")
    check(ns["SEED_METRICS"]["Eventhouse"]["status"] == "Failed",
          "foreign extents never receive a successful size measurement")


for n, f in list(globals().items()):
    if n.startswith("t_"):
        print("==", n)
        try:
            f()
        except Exception:
            traceback.print_exc(); FAIL.append(n)
print("RESULT:", "ALL PASSED" if not FAIL else f"{len(FAIL)} FAILED")
sys.exit(1 if FAIL else 0)
