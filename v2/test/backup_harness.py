"""Run the backup notebook's code cells against in-memory fakes of every service it calls."""
import base64, copy, hashlib, json, os, re, sys, time, types
from urllib.parse import urlsplit, unquote, parse_qs
import pandas as pd
import requests

NB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out", "V2_2_Backup_Data.ipynb")
G = lambda n: f"{n:08x}-0000-4000-8000-000000000000"
W1, W2, W3, W4 = G(1), G(2), G(3), G(4)


import io as _io, tarfile as _tarfile, subprocess as _subprocess


def _make_tar():
    buf = _io.BytesIO()
    with _tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"#!/bin/sh\necho fake\n"
        info = _tarfile.TarInfo("kusto-copy")
        info.size = len(data)
        tar.addfile(info, _io.BytesIO(data))
    return buf.getvalue()


KC_TAR = _make_tar()
KC_TAR_SHA = hashlib.sha256(KC_TAR).hexdigest()
ADX = "adx.westeurope.kusto.windows.net"


def sha(b):
    return hashlib.sha1(b"blob %d\x00" % len(b) + b).hexdigest()


class Resp:
    def __init__(self, status, body=None, headers=None, raw=None):
        self.status_code = status
        self.headers = {"Content-Type": "application/json; charset=utf-8", **(headers or {})}
        if raw is not None:
            self.content = raw
            self.headers["Content-Type"] = "application/octet-stream"
        else:
            self.content = b"" if body is None else json.dumps(body).encode()
        self.text = self.content.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.content)


def plat(t, name, logical):
    p = {"metadata": {"type": t, "displayName": name}, "config": {"version": "2.0"}}
    if logical is not None:
        p["config"]["logicalId"] = logical
    return json.dumps(p).encode()


class World:
    def __init__(self):
        self.calls = []
        self.fail_once = {"adls-append-503": True, "fabric-429": True}
        self.vanish = set()           # OneLake paths that 404 on read
        self.kql_fail = set()
        self.adls_forbidden = False
        self.push_conflict = False
        self.adls = {}                 # path -> bytes (flushed)
        self.adls_pending = {}
        self.ws = {W1: "Sales & Ops", W2: "Finance", W4: "Finance"}
        self.items = {W1: [], W2: [], W4: []}
        self.defs, self.lro = {}, set()
        self.def_forbidden, self.def_unsupported = set(), set()
        it = lambda ws, i, t, n: self.items[ws].append({"id": G(i), "type": t, "displayName": n, "workspaceId": ws})
        it(W1, 101, "Notebook", "N1"); it(W1, 102, "DataPipeline", "P1"); it(W1, 103, "Dataflow", "D1")
        it(W1, 104, "DataAgent", "A1"); it(W1, 105, "GraphModel", "G1"); it(W1, 106, "Ontology", "O1")
        it(W1, 107, "Lakehouse", "L1"); it(W1, 108, "SQLEndpoint", "L1"); it(W1, 109, "Warehouse", "WH1")
        it(W1, 110, "Eventhouse", "E1"); it(W1, 111, "KQLDatabase", "K1"); it(W1, 112, "Report", "R1")
        it(W1, 113, "Notebook", "N2 forbidden"); it(W1, 114, "Ontology", "O2 unsupported")
        it(W1, 115, "Notebook", "a/b:c")           # needs sanitizing
        it(W2, 201, "Notebook", "N1"); it(W2, 202, "KQLDatabase", "orphan KQL")  # parent not selected
        it(W4, 401, "Notebook", "Same ws name")
        if getattr(type(self), "with_staging", False):
            it(W1, 116, "Warehouse", "StagingWarehouseForDataflows_20261113112943")
            it(W1, 117, "Lakehouse", "StagingLakehouseForDataflows_20261113112943")
            it(W1, 118, "SQLEndpoint", "StagingLakehouseForDataflows_20261113112943")
        for ws, lst in self.items.items():
            for i in lst:
                if i["type"] in ("SQLEndpoint", "Report"):
                    continue
                n = int(i["id"][:8], 16)
                logical = [None, "00000000-0000-0000-0000-000000000000", G(9000 + n)][n % 3]
                self.defs[i["id"]] = {".platform": plat(i["type"], i["displayName"], logical),
                                      "content.txt": f"definition of {i['displayName']}".encode()}
        self.defs[G(109)]["dbo/Tables/F.sql"] = b"CREATE TABLE dbo.F (a int)"
        self.lro = {G(101), G(107), G(110)}
        self.def_forbidden = {G(113)}
        self.def_unsupported = {G(114)}
        big = bytes(range(256)) * 13  # 3328 bytes -> several chunks with CHUNK_BYTES=1000
        self.onelake = {  # (ws, item) -> {rel: bytes}
            (W1, G(107)): {"Files/a.csv": big, "Files/empty.txt": b"", "Files/sc/x.bin": b"shortcut data",
                           "Files/dir with space/ü.txt": b"unicode", "Tables/t1/part-0.parquet": b"P" * 1500,
                           "Tables/t1/_delta_log/00000000000000000000.json": b'{"commitInfo":{}}',
                           "Tables/ext/part.parquet": b"external"},
            (W1, G(109)): {"Tables/dbo/F/part-0.parquet": b"W" * 10,
                           "Tables/dbo/F/_delta_log/00000000000000000000.json": b"{}"},
        }
        self.hidden = set()               # OneLake files left out of folder listings (Warehouse data files)
        self.shortcuts = {G(107): [{"path": "Files", "name": "sc"}, {"path": "/Tables", "name": "ext"}]}
        self.kql = {G(111): {"T1": 100, "T 2": 0, "T3": 5}}
        self.ops = {}
        # Azure DevOps: main has one commit with an unrelated file
        self.ado_branches = {"main": {"files": {"/README.md": b"hi"}, "head": "c" * 40}}
        self.commit_n = 0
        self.sql_conn_attempts = []
        self.adx = {"K1db": {}}          # ADX database -> {table: rows}
        self.adx_schema = {}             # (db, table) -> create command
        self.kc_runs = []                # (table, staging, yaml)
        self.kc_fail = set()
        self.kc_short = set()
        self.kc_tar = KC_TAR
        self.sp_tokens = 0
        self.arm_actions = ["*"]         # the service principal's Azure actions on the cluster
        self.arm_puts = []               # (database, body)
        self.adx_viewers = []            # (database, principal)
        self.runner_token = None         # JWT for notebookutils getToken("pbi"), None = not a JWT

    # ------------------------------------------------------------ dispatcher
    def request(self, method, url, params=None, json=None, data=None, headers=None, timeout=None, **_):
        params = dict(params or {})
        u = urlsplit(url)
        for k, v in parse_qs(u.query).items():
            params.setdefault(k, v[0])
        host, path = u.netloc, unquote(u.path)
        self.calls.append((method, host, path))
        auth = (headers or {}).get("Authorization", "")
        if host == "dev.azure.com":
            assert auth.startswith("Basic "), auth
            assert base64.b64decode(auth[6:]).decode() == ":pat-secret"
            return self.devops(method, path, params, json)
        if host == "login.microsoftonline.com":
            assert path == "/tenant-1/oauth2/v2.0/token", path
            assert data["client_secret"] == "kc-s3cret" and data["client_id"] == "app-1", data
            assert data["scope"] in (f"https://{ADX}/.default", "https://management.azure.com/.default"), data
            self.sp_tokens += 1
            arm = data["scope"].startswith("https://management")
            return Resp(200, {"access_token": "arm-token" if arm else "sp-token", "expires_in": 3599})
        if host == "management.azure.com":
            assert auth == "Bearer arm-token", auth
            assert "api-version=" in url, url
            return self.arm_api(method, path, json)
        if host == "github.com":
            assert path == "/Azure/kusto-copy/releases/download/1.11.3.144/kusto-copy-linux.tar.gz", path
            assert not auth
            return Resp(200, raw=self.kc_tar)
        if host == ADX:
            assert auth == "Bearer " + "sp-token", auth
            return self.adx_api(url, json)
        assert auth.startswith("Bearer tok-") or auth == f"Bearer {self.runner_token}", (url, auth)
        if host == "api.fabric.microsoft.com":
            assert auth in ("Bearer tok-pbi", f"Bearer {self.runner_token}")
            return self.fabric(method, path, params, json)
        if host == "onelake.dfs.fabric.microsoft.com":
            assert auth == "Bearer tok-storage"
            return self.onelake_api(method, path, params, headers)
        if host == "acct.dfs.core.windows.net":
            assert auth == "Bearer tok-storage"
            return self.adls_api(method, path, params, data)
        if host == "k1.kusto.fabric.microsoft.com":
            assert auth == "Bearer tok-kusto"
            return self.kusto(json)
        raise AssertionError(f"unexpected call {method} {url}")

    # ------------------------------------------------------------ Fabric
    def fabric(self, method, path, params, body):
        p = path[len("/v1"):]
        if self.fail_once.pop("fabric-429", False):
            return Resp(429, {"errorCode": "TooManyRequests"}, {"Retry-After": "1"})
        m = re.fullmatch(r"/workspaces/([^/]+)", p)
        if m:
            ws = m.group(1)
            if ws not in self.ws:
                return Resp(403, {"errorCode": "InsufficientPrivileges", "message": "no access"})
            return Resp(200, {"id": ws, "displayName": self.ws[ws]})
        m = re.fullmatch(r"/workspaces/([^/]+)/items", p)
        if m:
            lst = self.items[m.group(1)]
            if "continuationToken" not in params:  # two pages
                return Resp(200, {"value": lst[:5], "continuationToken": "p2",
                                  "continuationUri": f"https://api.fabric.microsoft.com/v1{p}?continuationToken=p2"})
            return Resp(200, {"value": lst[5:]})
        m = re.fullmatch(r"/workspaces/([^/]+)/lakehouses", p)
        if m:
            props = {"sqlEndpointProperties": {"id": G(108), "connectionString": "x.datawarehouse.fabric.microsoft.com"}}
            if getattr(self, "lh_schema", ""):
                props["defaultSchema"] = self.lh_schema
            return Resp(200, {"value": [{"id": G(107), "properties": props}]})
        m = re.fullmatch(r"/workspaces/([^/]+)/kqlDatabases", p)
        if m:
            ws = m.group(1)
            v = [{"id": G(111), "properties": {"parentEventhouseItemId": G(110), "databaseName": "K1db",
                                                "queryServiceUri": "https://k1.kusto.fabric.microsoft.com"}}] if ws == W1 \
                else [{"id": G(202), "properties": {"parentEventhouseItemId": G(999)}}]
            return Resp(200, {"value": v})
        m = re.fullmatch(r"/workspaces/([^/]+)/items/([^/]+)/getDefinition", p)
        if m and method == "POST":
            iid = m.group(2)
            if iid in self.def_forbidden:
                return Resp(403, {"errorCode": "InsufficientPermissionsOverItem", "message": "nope"})
            if iid in self.def_unsupported:
                return Resp(400, {"errorCode": "OperationNotSupportedForItem", "message": "not supported"})
            result = {"definition": {"parts": [
                {"path": k, "payload": base64.b64encode(v).decode(), "payloadType": "InlineBase64"}
                for k, v in self.defs[iid].items()]}}
            if iid in self.lro:
                op = f"op-{iid}"
                self.ops[op] = {"polls": 0, "result": result}
                return Resp(202, None, {"Location": f"https://api.fabric.microsoft.com/v1/operations/{op}",
                                        "x-ms-operation-id": op, "Retry-After": "1"})
            return Resp(200, result)
        m = re.fullmatch(r"/operations/([^/]+)(/result)?", p)
        if m:
            op = self.ops[m.group(1)]
            if m.group(2):
                return Resp(200, op["result"])
            op["polls"] += 1
            return Resp(200, {"status": "Running" if op["polls"] < 2 else "Succeeded"})
        m = re.fullmatch(r"/workspaces/([^/]+)/items/([^/]+)/shortcuts", p)
        if m:
            return Resp(200, {"value": self.shortcuts.get(m.group(2), [])})
        raise AssertionError(f"unexpected Fabric call {method} {p}")

    # ------------------------------------------------------------ OneLake
    def onelake_api(self, method, path, params, headers):
        parts = path.strip("/").split("/", 2)
        ws = parts[0]
        if params.get("resource") == "filesystem":
            item, folder = params["directory"].split("/", 1)
            files = sorted((r, b) for r, b in self.onelake.get((ws, item), {}).items() if r.startswith(folder + "/")
                           and r not in self.hidden)
            if not files:
                return Resp(404, {"error": {"code": "PathNotFound", "message": "missing"}})
            start = int(params.get("continuation", 0))
            page = files[start:start + 3]
            entries = [{"name": f"{item}/{folder}", "isDirectory": "true"}] if start == 0 else []
            entries += [{"name": f"{item}/{r}", "contentLength": str(len(b)), "lastModified": "Tue, 17 Nov 2026 07:35:24 GMT"} for r, b in page]
            hdr = {"x-ms-continuation": str(start + 3)} if start + 3 < len(files) else {}
            return Resp(200, {"paths": entries}, hdr)
        item, rel = parts[1], parts[2]
        if rel in self.vanish:
            return Resp(404, {"error": {"code": "PathNotFound", "message": "gone"}})
        data = self.onelake[(ws, item)][rel]
        a, b = map(int, headers["Range"].split("=")[1].split("-"))
        assert b < len(data), "range past end"
        return Resp(206, raw=data[a:b + 1])

    # ------------------------------------------------------------ ADLS
    def adls_api(self, method, path, params, data):
        assert path == "/backup" or path.startswith("/backup/"), path
        p = path[len("/backup/"):]
        if self.adls_forbidden:
            return Resp(403, {"error": {"code": "AuthorizationPermissionMismatch", "message": "denied"}})
        if method == "PUT" and params.get("resource") == "file":
            self.adls_pending[p] = bytearray()
            self.adls.pop(p, None)
            return Resp(201)
        if method == "PATCH" and params.get("action") == "append":
            if self.fail_once.pop("adls-append-503", False):
                return Resp(503, {"error": {"code": "ServerBusy", "message": "busy"}})
            buf = self.adls_pending[p]
            assert int(params["position"]) == len(buf), ("bad position", p, params["position"], len(buf))
            buf.extend(data)
            return Resp(202)
        if method == "PATCH" and params.get("action") == "flush":
            buf = self.adls_pending.pop(p)
            assert int(params["position"]) == len(buf)
            self.adls[p] = bytes(buf)
            return Resp(200)
        if method == "GET" and p == "" and params.get("resource") == "filesystem":
            d = params["directory"].strip("/") + "/"
            names = sorted(k for k in self.adls if k.startswith(d))
            if not names:
                return Resp(404, {"error": {"code": "PathNotFound", "message": "not found"}})
            names = names[:int(params.get("maxResults", 5000))]
            return Resp(200, {"paths": [{"name": k, "contentLength": len(self.adls[k])} for k in names]})
        if method == "DELETE" and params.get("recursive") == "true":
            d = p.strip("/") + "/"
            gone = [k for k in self.adls if k.startswith(d)]
            if not gone:
                return Resp(404, {"error": {"code": "PathNotFound", "message": "not found"}})
            for k in gone:
                del self.adls[k]
            self.adls_deleted = getattr(self, "adls_deleted", []) + [p]
            return Resp(200)
        raise AssertionError(f"unexpected ADLS call {method} {path} {params}")

    # ------------------------------------------------------------ Kusto
    def kusto(self, body):
        db, csl = body["db"], body["csl"]
        assert db == "K1db", db
        tables = self.kql[G(111)]
        table = lambda cols, rows: Resp(200, {"Tables": [{"TableName": "Table_0",
                                                          "Columns": [{"ColumnName": c} for c in cols], "Rows": rows}]})
        if csl == ".show tables details":
            return table(["TableName", "TotalRowCount", "TotalOriginalSize"],
                         [[t, n, n * 10] for t, n in tables.items()])
        if csl == ".show database schema as csl script":
            return table(["DatabaseSchemaScript"], [[".create table T1 (a:int)"], [".create table ['T 2'] (b:string)"]])
        m = re.match(r'\.export async compressed to parquet \(h@"https://acct\.blob\.core\.windows\.net/backup/([^;"]+);impersonate"\) '
                     r"with \(namePrefix='([^']*)', parquetDatetimePrecision=\"microsecond\"\) <\| \['(.*)'\]$", csl)
        if m:
            dest, prefix, name = unquote(m.group(1)), m.group(2), m.group(3).replace("\\'", "'")
            op = f"kop-{len(self.ops)}"
            rows = tables[name]
            failed = name in self.kql_fail
            if not failed and rows:
                self.adls[f"{dest}/{prefix}_1.parquet"] = b"parquet:" + name.encode()
            self.ops[op] = {"state": "Failed" if failed else "Completed", "rows": rows, "polls": 0,
                            "path": f"{dest}/{prefix}_1.parquet"}
            return table(["OperationId"], [[op]])
        m = re.fullmatch(r"\.show operations (\S+)", csl)
        if m:
            op = self.ops[m.group(1)]
            op["polls"] += 1
            st = "InProgress" if op["polls"] < 2 else op["state"]
            return table(["OperationId", "State", "Status"], [[m.group(1), st, "boom" if st == "Failed" else ""]])
        m = re.fullmatch(r"\.show operation (\S+) details", csl)
        if m:
            op = self.ops[m.group(1)]
            return table(["Path", "NumRecords", "SizeInBytes"], [[op["path"], op["rows"], 99]] if op["rows"] else [])
        m = re.fullmatch(r"\.show table \['(.*)'\] cslschema", csl)
        if m:
            name = m.group(1).replace("\\'", "'")
            assert name in tables, name
            return table(["TableName", "Schema", "DatabaseName", "Folder", "DocString"],
                         [[name, "a:int, b:string", "K1db", "raw" if name == "T1" else "", ""]])
        raise AssertionError("unexpected KQL " + csl)

    CID = "/subscriptions/sub-1/resourceGroups/rg-1/providers/Microsoft.Kusto/clusters/adx"

    def arm_api(self, method, path, body):
        cid = self.CID
        if method == "GET" and path == "/subscriptions":
            return Resp(200, {"value": [{"subscriptionId": "sub-0"}, {"subscriptionId": "sub-1"}]})
        if method == "GET" and path == "/subscriptions/sub-0/providers/Microsoft.Kusto/clusters":
            return Resp(200, {"value": []})
        if method == "GET" and path == "/subscriptions/sub-1/providers/Microsoft.Kusto/clusters":
            return Resp(200, {"value": [{"id": cid, "name": "adx", "location": "westeurope",
                                         "properties": {"uri": f"https://{ADX.upper()}"}}]})
        if method == "GET" and path == cid:
            return Resp(200, {"id": cid, "name": "adx", "location": "westeurope",
                              "properties": {"uri": f"https://{ADX}"}})
        if method == "GET" and path == cid + "/providers/Microsoft.Authorization/permissions":
            return Resp(200, {"value": [{"actions": self.arm_actions, "notActions": []}]})
        if method == "GET" and path == cid + "/databases":
            return Resp(200, {"value": [{"name": f"adx/{d}"} for d in self.adx]})
        if method == "PUT" and path.startswith(cid + "/databases/"):
            db = path[len(cid + "/databases/"):]
            assert body["kind"] == "ReadWrite" and body["location"] == "westeurope", body
            self.arm_puts.append((db, body))
            self.adx[db] = {}
            return Resp(202, {}, {"Azure-AsyncOperation": "https://management.azure.com/ops/1?api-version=2023-08-15"})
        if method == "GET" and path == "/ops/1":
            return Resp(200, {"status": "Succeeded"})
        raise AssertionError(f"unexpected ARM {method} {path}")

    def adx_api(self, url, body):
        db, csl = body["db"], body["csl"]
        m = re.fullmatch(r"\.add database \['(.*)'\] viewers \('(.*)'\) 'Fabric migration backup \(V2_2\)'", csl)
        if m:
            assert m.group(1) == db and db in self.adx, csl
            self.adx_viewers.append((db, m.group(2)))
            return Resp(200, {"Tables": []})
        table = lambda cols, rows: Resp(200, {"Tables": [{"TableName": "Table_0",
                                                          "Columns": [{"ColumnName": c} for c in cols], "Rows": rows}]})
        if db not in self.adx:
            return Resp(400, {"error": {"code": "BadRequest_EntityNotFound", "message": f"database {db} not found"}})
        if url.endswith("/v1/rest/mgmt") and csl == ".show tables details":
            return table(["TableName", "DatabaseName", "TotalRowCount"], [[t, db, n] for t, n in self.adx[db].items()])
        m = re.fullmatch(r"\.create-merge table \['(.*)'\] \((.*?)\)( with \(folder='raw'\))?", csl)
        if m and url.endswith("/v1/rest/mgmt"):
            name = m.group(1).replace("\\'", "'")
            assert m.group(2) == "a:int, b:string"
            assert (m.group(3) is not None) == (name == "T1"), csl
            self.adx[db].setdefault(name, 0)
            self.adx_schema[(db, name)] = csl
            return table(["TableName"], [[name]])
        m = re.fullmatch(r"\['(.*)'\] \| count", csl)
        if m and url.endswith("/v1/rest/query"):
            return table(["Count"], [[self.adx[db][m.group(1).replace("\\'", "'")]]])
        raise AssertionError("unexpected ADX " + url + " " + csl)

    def popen(self, args, cwd=None, env=None, **kw):
        assert args[1] == "-y" and os.access(args[0], os.X_OK), args
        assert env["AZURE_CLIENT_SECRET"] == "kc-s3cret" and env["AZURE_TENANT_ID"] == "tenant-1"
        assert env["AZURE_CLIENT_ID"] == "app-1"
        assert env["DOTNET_SYSTEM_GLOBALIZATION_PREDEFINED_CULTURES_ONLY"] == "false", "culture crash fix missing"
        assert env["DOTNET_SYSTEM_GLOBALIZATION_INVARIANT"] in ("0", "1")
        assert "kc-s3cret" not in " ".join(args)
        text = open(args[2], encoding="utf-8").read()
        assert "kc-s3cret" not in text
        val = lambda key: json.loads(re.search(rf"{key}: (.*)", text).group(1))
        tbl = val("tableName")
        staging = json.loads(re.search(r"stagingStorageDirectories:\n  - (.*)", text).group(1))
        assert val("clusterUri") == "https://k1.kusto.fabric.microsoft.com"
        assert f'clusterUri: "https://{ADX}"' in text and val("copyMode") == "BackfillAndNew", text
        self.kc_runs.append((tbl, staging, text))
        code = 1 if tbl in self.kc_fail else 0
        if code == 0:   # BackfillAndNew: progress in the staging folder; rows already copied aren't copied again
            dest_db = json.loads(re.findall(r"databaseName: (.*)", text)[1])
            sp = staging.split("/backup/", 1)[1]
            done = int(self.adls.get(sp + "/progress.json", b"0"))
            total = self.kql[G(111)][tbl] - (1 if tbl in self.kc_short else 0)
            self.adx[dest_db][tbl] = self.adx[dest_db].get(tbl, 0) + max(0, total - done)
            self.adls[sp + "/progress.json"] = str(total).encode()
        out = ["\x1b[32mKusto Copy 1.11.3.144\x1b[0m\n",
               f"Exporting to {staging}/x.parquet?sv=2024&sig=TOPSECRETSIG\n",
               "debug: secret is kc-s3cret\n"]
        if code:
            out.append("Error: ingestion failed for block 3\n")

        class P:
            stdout = iter(out)
            returncode = code

            def wait(self_inner):
                return code

            def kill(self_inner):
                pass
        return P()

    # ------------------------------------------------------------ Azure DevOps
    def devops(self, method, path, params, body):
        assert params.get("api-version") == "7.1", params
        base = "/org/proj/_apis/git/repositories/repo"
        assert path.startswith(base), path
        p = path[len(base):]
        if p == "" and method == "GET":
            return Resp(200, {"id": "r", "name": "repo", "defaultBranch": "refs/heads/main"})
        if p == "/refs":
            name = params["filter"].replace("heads/", "", 1)
            b = self.ado_branches.get(name)
            return Resp(200, {"value": [{"name": f"refs/heads/{name}", "objectId": b["head"]}] if b else []})
        if p == "/items":
            br = self.ado_branches[params["versionDescriptor.version"]]["files"]
            if "path" in params:
                return Resp(200, {"path": params["path"], "content": br[params["path"]].decode()})
            scope = params["scopePath"]
            blobs = [{"path": k, "objectId": sha(v), "gitObjectType": "blob"} for k, v in br.items()
                     if k.startswith(scope + "/")]
            if not blobs:
                return Resp(404, {"message": "TF401174: The item could not be found", "typeKey": "GitItemNotFoundException"})
            return Resp(200, {"value": [{"path": scope, "gitObjectType": "tree"}] + blobs})
        if p == "/pushes" and method == "POST":
            ref = body["refUpdates"][0]
            name = ref["name"].replace("refs/heads/", "")
            if self.push_conflict:
                return Resp(409, {"message": "TF401028: The reference has already been updated by another client",
                                  "typeKey": "GitReferenceStaleException"})
            if name in self.ado_branches:
                branch = self.ado_branches[name]
                if ref["oldObjectId"] != branch["head"]:
                    return Resp(409, {"message": "TF401028 stale", "typeKey": "GitReferenceStaleException"})
                files = branch["files"]
            else:  # new branch from the commit named in oldObjectId
                src = next(b for b in self.ado_branches.values() if b["head"] == ref["oldObjectId"])
                files = dict(src["files"])
            for ch in body["commits"][0]["changes"]:
                pth, kind = ch["item"]["path"], ch["changeType"]
                if kind == "add":
                    assert pth not in files, ("add of existing", pth)
                elif kind in ("edit", "delete"):
                    assert pth in files, (kind + " of missing", pth)
                if kind == "delete":
                    del files[pth]
                else:
                    assert ch["newContent"]["contentType"] == "base64encoded"
                    files[pth] = base64.b64decode(ch["newContent"]["content"])
            self.commit_n += 1
            head = f"{self.commit_n:040x}"
            self.ado_branches[name] = {"files": files, "head": head}
            return Resp(201, {"commits": [{"commitId": head}]})
        raise AssertionError(f"unexpected DevOps call {method} {p}")


class FakeConn:
    def __init__(self, empty=False):
        self.empty = empty

    def query(self, sql):
        if self.empty:
            return None                            # what the real connector returns for no rows
        if "sql_modules" in sql:
            return pd.DataFrame([["dbo", "v1", "VIEW", "CREATE VIEW dbo.v1 AS SELECT 1"],
                                 ["dbo", "f/1", "SQL_SCALAR_FUNCTION", "CREATE FUNCTION ..."]],
                                columns=["schema_name", "object_name", "type_desc", "definition"])
        return pd.DataFrame([["user@x", "EXTERNAL_USER", "DATABASE", "SELECT", "GRANT", None, None]],
                            columns=["principal_name", "principal_type", "class_desc", "permission_name",
                                     "state_desc", "schema_name", "object_name"])


def make_notebookutils(world):
    nu = types.ModuleType("notebookutils")
    nu.credentials = types.SimpleNamespace(
        getToken=lambda aud: (world.runner_token if aud == "pbi" and world.runner_token else f"tok-{aud}"),
        getSecret=lambda vault, name: {("https://kv.vault.azure.net/", "ado-pat"): "pat-secret",
                                       ("https://kv.vault.azure.net/", "kc-secret"): "kc-s3cret"}[(vault, name)])

    def connect(artifact, workspace, kind):
        world.sql_conn_attempts.append((artifact, kind))
        if kind == "SQLEndpoint":
            raise RuntimeError("type not supported")
        return FakeConn(getattr(world, "sql_empty", False))
    nu.data = types.SimpleNamespace(connect_to_artifact=connect)
    return nu


SETTINGS = f"""
WORKSPACE_IDS = ["{W1.upper()}", "{{{W2}}}", "{W3}", "{W4}", "{W1}"]
DRY_RUN = False
ADLS_ACCOUNT = "acct"; ADLS_CONTAINER = "backup"; ADLS_ROOT = "/fb/"
ADO_ORGANIZATION = "org"; ADO_PROJECT = "proj"; ADO_REPOSITORY = "repo"
KEY_VAULT_URL = "https://kv.vault.azure.net/"; ADO_PAT_SECRET_NAME = "ado-pat"
ADX_CLUSTER_URI = "https://{ADX}/"; KC_TENANT_ID = "tenant-1"; KC_CLIENT_ID = "app-1"
KC_CLIENT_SECRET_NAME = "kc-secret"; KC_SHA256 = "{KC_TAR_SHA}"
ADX_DATABASE_PATTERN = "{{database}}"; ADX_CREATE_DATABASES = False
"""


def run(world, extra="", run_id=None, stop_after=None, after_helpers=""):
    """Execute all code cells; returns the namespace (or raises the cell's exception)."""
    sys.modules["notebookutils"] = make_notebookutils(world)
    requests.Session.request = lambda self, method, url, **kw: world.request(method, url, **kw)
    time.sleep = lambda s: None
    _subprocess.Popen = world.popen
    nb = json.load(open(NB, encoding="utf-8"))
    code = [("".join(c["source"])) for c in nb["cells"] if c["cell_type"] == "code"]
    ns = {"__name__": "__main__", "display": lambda df: None}
    for i, src in enumerate(code):
        exec(compile(src, f"cell{i}", "exec"), ns)
        if i == 0:
            exec(SETTINGS + extra, ns)
        if i == 1:
            ns["CHUNK_BYTES"] = 1000
            exec(after_helpers, ns)
            if run_id:
                ns["RUN_ID"] = run_id
        if stop_after is not None and i == stop_after:
            break
    return ns
