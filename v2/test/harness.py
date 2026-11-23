"""Fake Fabric (items + Git integration), Azure DevOps Git, ADLS and Key Vault for the v2 notebooks."""
import base64, hashlib, io, itertools, json, os, re, sys, types
from urllib.parse import urlsplit, parse_qs, unquote
import pandas as pd
import requests

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out")
G = lambda n: f"{n:08x}-0000-4000-8000-{n:012x}"
W1, W2, CAP = G(1), G(2), G(3)
RUN = "20261120T190000Z"
DIR = "/fabric/sales"


class Resp:
    def __init__(self, status, body=None, headers=None, raw=None):
        self.status_code = status
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.content = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
        self.text = self.content.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.content)


def err(status, code, msg="x"):
    return Resp(status, {"errorCode": code, "message": msg})


def delta_log(part, rows):
    return (json.dumps({"metaData": {"configuration": {}}}) + "\n" +
            json.dumps({"add": {"path": part, "stats": json.dumps({"numRecords": rows})}}) + "\n").encode()


DATA_FILES = {
    "Lakehouse": {"Files/raw/a.csv": b"a,b\n1,2\n", "Tables/sales/part-0.parquet": b"Q" * 20,
                  "Tables/sales/_delta_log/00000000000000000000.json": delta_log("part-0.parquet", 5)},
    "Warehouse": {"Tables/dbo/orders/part-0.parquet": b"W" * 25,
                  "Tables/dbo/orders/_delta_log/00000000000000000000.json": delta_log("part-0.parquet", 7)},
}


class World:
    def __init__(self):
        self.ids = itertools.count(100)
        self.adls, self.pending, self.calls, self.ops = {}, {}, [], {}
        self.ws = {}
        self.defs = {}
        self.commits, self.refs, self.locked = {}, {}, set()
        self.update_bodies = []
        self.onelake, self.onelake_pending, self.kusto, self.wh_rows, self.sql_objects = {}, {}, {}, {}, {}
        self.sql_batches, self.parquet_rows, self.wh_data = [], {}, {}
        self.kusto_log, self.policies = [], {}
        self.roles, self.tags = {}, {}
        w = self.add_ws(W1, "Sales")
        self.LH = self.add_item(W1, "Lakehouse", "LH", "lakehouse")
        self.WH = self.add_item(W1, "Warehouse", "WH", "warehouse")
        self.EH = self.add_item(W1, "Eventhouse", "EH", "eventhouse")
        self.KQ = self.add_item(W1, "KQLDatabase", "EH", "kql")
        self.NB = self.add_item(W1, "Notebook", "NB", f"default_lakehouse {self.LH} ws {W1}")
        self.PL = self.add_item(W1, "DataPipeline", "PL", "pipeline")
        self.DA = self.add_item(W1, "DataAgent", "DA", "agent")
        self.SM = self.add_item(W1, "SemanticModel", "SM", f"Sql.Database(x, {self.ep(self.LH)})")
        self.RP = self.add_item(W1, "Report", "RP", "report")
        # connected to Git and in sync
        sha = self.commit({self.path(i): c for i, c in self.git_view(W1).items()}, None)
        self.refs["main"] = sha
        w["git"] = {"state": "ConnectedAndInitialized", "branch": "main", "dir": DIR, "head": sha}
        self.make_backup()

    # ---------------- model
    def jwt(self):
        claims = getattr(self, "claims", {"oid": "me-oid", "upn": "admin@migration.example", "idtyp": "user",
                                                 "tid": "tenant-example"})
        enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
        return enc({"alg": "none"}) + "." + enc(claims) + ".sig"

    def new_id(self):
        return G(next(self.ids))

    def add_ws(self, wid, name):
        self.ws[wid] = {"name": name, "capacityId": CAP, "items": [], "git": {"state": "NotConnected"}}
        return self.ws[wid]

    def add_item(self, wid, t, name, content, logical=None):
        iid = self.new_id()
        it = {"id": iid, "type": t, "displayName": name, "logicalId": logical or self.new_id()}
        self.ws[wid]["items"].append(it)
        self.defs[iid] = content
        if t == "Lakehouse":
            ep = {"id": self.new_id(), "type": "SQLEndpoint", "displayName": name, "parent": iid}
            self.ws[wid]["items"].append(ep)
        return iid

    def ep(self, lh):
        return next(i["id"] for w in self.ws.values() for i in w["items"] if i.get("parent") == lh)

    def item(self, wid, iid):
        return next((i for i in self.ws[wid]["items"] if i["id"] == iid), None)

    def path(self, key):
        t, n = key.split("|")
        return f"{DIR}/{n}.{t}"

    def git_view(self, wid):
        """{type|name: content} of git-supported items in the workspace."""
        return {f"{i['type']}|{i['displayName']}": self.defs[i["id"]] + "|" + i["logicalId"]
                for i in self.ws[wid]["items"]
                if i["type"] != "SQLEndpoint" and i["type"] not in getattr(self, "git_unsupported", ())
                and f"{i['type']}|{i['displayName']}" not in getattr(self, "git_unsupported_items", ())
                and not re.match(r"^Staging(Lakehouse|Warehouse)ForDataflows", i["displayName"])}

    def tree_view(self, sha, d=DIR):
        files = self.commits[sha]
        out = {}
        for p, b in files.items():
            if p.startswith(d.rstrip("/") + "/") and p.endswith("/.platform"):
                meta = json.loads(b)
                folder = p[:-len("/.platform")]
                out[f"{meta['metadata']['type']}|{meta['metadata']['displayName']}"] = \
                    files[folder + "/content.txt"].decode() + "|" + meta["config"]["logicalId"]
        return out

    def commit(self, folders, parent):
        files = {}
        for folder, content in folders.items():
            c, lid = content.rsplit("|", 1)
            name, t = folder.rsplit("/", 1)[1].rsplit(".", 1)
            files[folder + "/.platform"] = json.dumps({"metadata": {"type": t, "displayName": name},
                                                       "config": {"logicalId": lid}}).encode()
            files[folder + "/content.txt"] = c.encode()
        sha = hashlib.sha1((json.dumps(sorted((k, v.decode()) for k, v in files.items())) + str(parent) +
                            str(next(self.ids))).encode()).hexdigest()
        self.commits[sha] = files
        return sha

    def changes(self, wid):
        g = self.ws[wid]["git"]
        base = self.tree_view(g["head"], g["dir"]) if g.get("head") else {}
        remote = self.tree_view(self.refs[g["branch"]], g["dir"])
        cur = self.git_view(wid)
        out = []
        for k in sorted(set(base) | set(remote) | set(cur)):
            b, r, c = base.get(k), remote.get(k), cur.get(k)
            wc = None if c == b else ("Added" if b is None else "Deleted" if c is None else "Modified")
            rc = None if r == b else ("Added" if b is None else "Deleted" if r is None else "Modified")
            if not wc and not rc:
                continue
            t, n = k.split("|")
            ch = {"itemMetadata": {"itemType": t, "displayName": n, "itemIdentifier": {
                "objectId": next((i["id"] for i in self.ws[wid]["items"] if f"{i['type']}|{i['displayName']}" == k), None)}},
                "conflictType": "Conflict" if wc and rc else "None"}
            if wc:
                ch["workspaceChange"] = wc
            if rc:
                ch["remoteChange"] = rc
            out.append(ch)
        return out

    def make_backup(self, data_ok=True):
        root = f"fabric-backups/{RUN}"
        self.adls[f"{root}/_run_started.json"] = json.dumps({"startedUtc": "2026-11-20T19:00:00+00:00"}).encode()
        self.adls[f"{root}/_run_completed.json"] = b"{}"
        rows = []
        for i in self.ws[W1]["items"]:
            if i["type"] in ("Report", "SemanticModel"):
                continue
            if i["type"] == "SQLEndpoint":
                rows.append({"ItemId": i["id"], "Step": "SQL scripts", "Status": "OK"})
                sfolder = f"{root}/Sales__{W1}/{i['displayName']}.{i['type']}__{i['id']}"
                self.adls[f"{sfolder}/sql/dbo/VIEW/v_sales.sql"] = b"CREATE VIEW dbo.v_sales AS SELECT 1 AS x"
                self.adls[f"{sfolder}/item.json"] = b"{}"
                continue
            rows.append({"ItemId": i["id"], "Step": "Definition", "Status": "OK"})
            self.adls[f"{root}/Sales__{W1}/{i['displayName']}.{i['type']}__{i['id']}/definition/x.json"] = b"{}"
            folder = f"{root}/Sales__{W1}/{i['displayName']}.{i['type']}__{i['id']}"
            if i["type"] in ("Lakehouse", "Warehouse"):
                rows.append({"ItemId": i["id"], "Step": "Data", "Status": "OK" if data_ok else "Partial"})
                files = DATA_FILES[i["type"]]
                for rel, b in files.items():
                    self.adls[f"{folder}/data/{rel}"] = b
                self.adls[f"{folder}/data_manifest.csv"] = ("path,size,lastModified,status\n" + "".join(
                    (f"{rel},{len(b)},Tue, 17 Nov 2026 07:35:24 GMT,copied\n" if getattr(self, "old_manifest", True) else
                     f'"{rel}","{len(b)}","Tue, 17 Nov 2026 07:35:24 GMT","copied"\n') for rel, b in files.items())).encode()
            if i["type"] == "KQLDatabase":
                rows.append({"ItemId": i["id"], "Step": "KQL export", "Status": "OK", "Rows": "12"})
                self.adls[f"{folder}/data/Events/Events_1.parquet"] = b"P" * 30
                self.adls[f"{folder}/database_schema.kql"] = b".create-merge table Events (x:string)"
            if i["type"] == "SQLEndpoint":
                sfolder = f"{root}/Sales__{W1}/{i['displayName']}.{i['type']}__{i['id']}"
                self.adls[f"{sfolder}/sql/dbo/VIEW/v_sales.sql"] = b"CREATE VIEW dbo.v_sales AS SELECT 1 AS x"
                self.adls[f"{sfolder}/item.json"] = b"{}"
        self.adls[f"{root}/backup_log.csv"] = pd.DataFrame(rows).to_csv(index=False).encode()

    def kql_to_adx(self, keep_parquet=False):
        """The backup ran with KQL_BACKUP_MODE = "adx" (or "both" with keep_parquet): rows in ADX via Kusto Copy."""
        key = next(k for k in self.adls if k.endswith("/backup_log.csv") and "_migration" not in k)
        log = pd.read_csv(io.BytesIO(self.adls[key]), dtype=str).fillna("")
        kq = log[log.Step == "KQL export"].iloc[0].ItemId
        if not keep_parquet:
            log = log[log.Step != "KQL export"]
            for k in [k for k in self.adls if kq in k and "/data/" in k]:
                del self.adls[k]
        log = pd.concat([log, pd.DataFrame([{"ItemId": kq, "Step": "Kusto Copy", "Status": "OK", "Rows": "12"}])])
        self.adls[key] = log.to_csv(index=False).encode()
        folder = next(k for k in self.adls if kq in k and k.endswith("/database_schema.kql")).rsplit("/", 1)[0]
        self.adls[f"{folder}/kusto_copy/_destination.json"] = json.dumps({
            "cluster": "https://adx.example", "database": "K1db", "status": "OK",
            "tables": {"Events": {"status": "OK", "rows": 12}}}).encode()

    def migrate(self, new_id=None):
        """What the delete notebook + region move leave: Power BI items only, Git disconnected."""
        w = self.ws.pop(W1) if new_id else self.ws[W1]
        keep = [i for i in w["items"] if i["type"] in ("Report", "SemanticModel")]
        if new_id:
            self.add_ws(new_id, "Sales")["items"] = keep
        else:
            w["items"] = keep
            w["git"] = {"state": "NotConnected"}

    # ---------------- HTTP
    def request(self, method, url, params=None, json=None, data=None, headers=None, timeout=None, **_):
        u = urlsplit(url)
        params = {**{k: v[0] for k, v in parse_qs(u.query).items()}, **(params or {})}
        host, path = u.hostname, unquote(u.path)
        self.calls.append((method, host, path, json))
        if host == "acct.dfs.core.windows.net":
            return self.dfs(method, path[len("/fabric-backup"):].lstrip("/"), params, data)
        if host == "api.fabric.microsoft.com":
            return self.fabric(method, path[len("/v1"):], json, params)
        if host == "login.microsoftonline.com":
            self.sp_logins = getattr(self, "sp_logins", 0) + 1
            assert method == "POST" and data["grant_type"] == "client_credentials" and \
                data["scope"] == "499b84ac-1321-427f-aa17-267ca6975798/.default", data
            if data["client_secret"] != getattr(self, "sp_secret", "abc8Q~" + "s" * 34) or path != "/tenant-example/oauth2/v2.0/token":
                return Resp(401, {"error": "invalid_client",
                                  "error_description": "AADSTS7000215: Invalid client secret provided."})
            return Resp(200, {"access_token": "sp-token", "expires_in": getattr(self, "sp_expires_in", 3599)})
        if host == "dev.azure.com":
            auth = (headers or {}).get("Authorization", "")
            if not auth:                                   # anonymous call: DevOps names its Entra tenant
                if getattr(self, "org_tenant_hidden", False):
                    return Resp(200, {"authenticatedUser": {}})
                return Resp(401, raw=b"", headers={"X-VSS-ResourceTenant": getattr(self, "org_tenant", "tenant-example")})
            if auth.startswith("Bearer"):
                assert auth == "Bearer sp-token", auth
                if getattr(self, "sp_not_in_org", False):
                    return err(401, "Unauthorized", "TF400813: The user is not authorized to access this resource.")
            return self.devops(method, path, params, json)
        if host == "api.powerbi.com":
            m = re.match(r"^/v1.0/myorg/admin/groups/([^/]+)/users$", path)
            assert method == "POST" and m and json["groupUserAccessRight"] == "Admin", (method, path, json)
            self.roles.setdefault(m.group(1), {})[json["identifier"]] = "Admin"
            return Resp(200)
        if host == "onelake.dfs.fabric.microsoft.com":
            return self.onelake_req(method, path.lstrip("/"), params, data, headers or {})
        if host == "kusto.example":
            return self.kusto_req(path, json)
        raise AssertionError(f"unexpected {method} {url}")

    def abfss(self, url):
        m = re.match(r"^abfss://([^@]+)@([^/]+)/?(.*)$", url)
        c, host, p = m.groups()
        if host == "onelake.dfs.fabric.microsoft.com":
            return self.onelake, f"{c}/{p}".rstrip("/")
        return self.adls, p.rstrip("/")

    def fastcp(self, src, dst, recurse=True, extraConfigs=None):
        """notebookutils.fs.fastcp as azcopy does it: folder T copied to P lands in P/T ("into"), or in P itself
        with fastcp_mode "contents". fastcp_fail: raise; fastcp_drop: silently leave those relative paths out."""
        self.fastcp_calls = getattr(self, "fastcp_calls", []) + [(src, dst, extraConfigs)]
        if getattr(self, "fastcp_fail", False):
            raise Exception("azcopy: simulated failure")
        flags = (extraConfigs or {}).get("flags", "")
        excl = re.findall(r"--exclude-path=(\S+)", flags)
        excl = [] if getattr(self, "fastcp_ignore_exclude", False) else excl
        keep = "--overwrite=false" in flags
        s_store, s_key = self.abfss(src)
        d_store, d_key = self.abfss(dst)
        name = s_key.rsplit("/", 1)[-1]
        base = (d_key + "/" + name) if getattr(self, "fastcp_mode", "into") == "into" else d_key
        for k in sorted(k for k in list(s_store) if k.startswith(s_key + "/")):
            rel = k[len(s_key) + 1:]
            if any(rel == e or rel.startswith(e + "/") for e in excl) or rel in getattr(self, "fastcp_drop", ()):
                continue
            t = base + "/" + rel
            if keep and t in d_store:
                continue
            d_store[t] = s_store[k]
            if d_store is self.onelake:
                self.onelake_order = getattr(self, "onelake_order", []) + [t]
        return True

    def dfs(self, method, path, params, data):
        if method == "GET" and params.get("resource") == "filesystem":
            d = params["directory"].strip("/") + "/"
            rec = params["recursive"] == "true"
            out, names = [], set()
            for p in self.adls:
                if not p.startswith(d):
                    continue
                parts = p[len(d):].split("/")
                for k in range(1, len(parts)):
                    dn = d + "/".join(parts[:k])
                    if (rec or k == 1) and dn not in names:
                        names.add(dn); out.append({"name": dn, "isDirectory": "true"})
                if rec or len(parts) == 1:
                    out.append({"name": p, "contentLength": str(len(self.adls[p]))})
            return Resp(200, {"paths": out}) if out else err(404, "PathNotFound")
        if method == "HEAD":
            return Resp(200) if path in self.adls else Resp(404)
        if method == "GET":
            return Resp(200, raw=self.adls[path]) if path in self.adls else err(404, "PathNotFound")
        if method == "PUT":
            self.pending[path] = b""; return Resp(201)
        if method == "PATCH":
            if params["action"] == "append":
                self.pending[path] += data; return Resp(202)
            self.adls[path] = self.pending.pop(path); return Resp(200)
        if method == "DELETE" and params.get("recursive") == "true":
            for k in [k for k in self.adls if k == path or k.startswith(path.rstrip("/") + "/")]:
                del self.adls[k]
            return Resp(200)
        if method == "DELETE":
            self.adls.pop(path); return Resp(200)
        raise AssertionError((method, path))

    def onelake_req(self, method, path, params, data, headers):
        if method == "GET" and params.get("resource") == "filesystem":
            ws = path
            d = params["directory"].strip("/") + "/"
            out = [{"name": k[len(ws) + 1:], "contentLength": str(len(v))} for k, v in self.onelake.items()
                   if k.startswith(f"{ws}/{d}")]
            return Resp(200, {"paths": out}) if out else err(404, "PathNotFound")
        if method == "HEAD":
            return Resp(200, headers={"Content-Length": str(len(self.onelake[path]))}) if path in self.onelake else Resp(404)
        if method == "PUT" and params.get("resource") == "directory":
            self.onelake_dirs = getattr(self, "onelake_dirs", set()) | {path}
            return Resp(201)
        if method == "PUT":
            if headers.get("If-None-Match") == "*" and path in self.onelake:
                return err(409, "PathAlreadyExists")
            self.onelake_pending[path] = b""
            self.onelake_order = getattr(self, "onelake_order", []) + [path]
            return Resp(201)
        if method == "DELETE":
            self.onelake.pop(path)
            return Resp(200)
        if method == "PATCH":
            if params["action"] == "append":
                self.onelake_pending[path] += data
                return Resp(202)
            self.onelake[path] = self.onelake_pending.pop(path)
            return Resp(200)
        raise AssertionError((method, path))

    def kusto_req(self, path, body):
        db, csl = body["db"], body["csl"]
        tables = self.kusto.setdefault(db, {"Events": 0})
        self.kusto_log.append(csl)
        def table(rows, cols):
            return Resp(200, {"Tables": [{"Columns": [{"ColumnName": c} for c in cols], "Rows": rows}]})
        if csl == ".show tables":
            return table([[t] for t in tables], ["TableName"])
        if csl == ".show table * policy update":
            return table([[f"[{db}].[{t}]", p] for t, p in self.policies.get(db, {}).items()], ["EntityName", "Policy"])
        if csl.startswith(".delete table") or csl.startswith(".alter table"):
            return table([], [])
        if csl.startswith(".execute database script"):
            tables.setdefault("Events", 0)
            return table([], [])
        m = re.match(r"^\.ingest async into table \['(.+?)'\] \((.*)\) with", csl, re.S)
        if m:
            tables[m.group(1)] += 12 * m.group(2).count("h'")
            return table([["op1"]], ["OperationId"])
        m = re.match(r"^\.set-or-append async \['(.+?)'\] <\| cluster\('(.+?)'\)\.database\('(.+?)'\)\.\['(.+?)'\]$", csl)
        if m:
            self.adx_copies = getattr(self, "adx_copies", []) + [m.groups()]
            tables[m.group(1)] += 12
            return table([["op2"]], ["OperationId"])
        if csl.startswith(".show operations"):
            return table([["Completed", ""]], ["State", "Status"])
        if csl.startswith("externaldata"):
            ok = re.search(r"h'https://acct\.blob\.core\.windows\.net/fabric-backup/(.+?);impersonate'", csl)
            return table([[1 if ok and ok.group(1) in self.adls else 0]], ["Count"])
        m = re.match(r"^\['(.+?)'\] \| count$", csl)
        if m:
            return table([[tables.get(m.group(1), 0)]], ["Count"])
        raise AssertionError(csl)

    def sql_conn(self, item_id):
        world = self

        class Conn:
            def query(self, text):
                command = re.fullmatch(r"EXEC\(N'(.*)'\); SELECT 1 AS done;", text, re.S)
                if command:
                    world.sql_exec(item_id, command[1].replace("''", "'"))
                    return pd.DataFrame({"done": [1]})
                if "OPENROWSET" in text:
                    return pd.DataFrame({"n": [1]})
                m = re.search(r"FROM \[(.+?)\]\.\[(.+?)\]$", text)
                if m:
                    key = (item_id, f"{m.group(1)}.{m.group(2)}")
                    result = {"n": [world.wh_rows.get(key, 0)]}
                    cols = re.findall(r"COUNT_BIG\(\[((?:[^\]]|\]\])*)\]\)", text)
                    for j, col in enumerate(cols):
                        result[f"c{j}"] = [sum(r.get(col.replace("]]", "]")) is not None
                                                for r in world.wh_data.get(key, []))]
                    return pd.DataFrame(result)
                return pd.DataFrame({"k": sorted(world.sql_objects.get(item_id, set()))})
        return Conn()

    def sql_exec(self, item_id, batch):
        if batch.strip().rstrip(";") == "SELECT 1":
            return
        self.sql_batches.append((item_id, batch))
        ident = r"\[((?:[^\]]|\]\])*)\]"
        m = re.match(r"^INSERT INTO " + ident + r"\." + ident + r"\s*\((.*?)\)\s+SELECT", batch, re.S)
        if m:
            key = (item_id, f"{m.group(1)}.{m.group(2)}")
            targets = [n.replace("]]", "]") for n in re.findall(ident, m.group(3))]
            sources = re.findall(r"SELECT\s+(.*?)\s+FROM OPENROWSET\(BULK '([^']+)', FORMAT = 'PARQUET'\)"
                                 r"\s+WITH\s*\((.*?)\)\s+AS \[src\]", batch, re.S)
            assert sources, batch
            for selection, url, schema in sources:
                selected = [n.replace("]]", "]") for n in re.findall(r"\[src\]\." + ident, selection)]
                declared = {n.replace("]]", "]") for n in re.findall(ident + r"\s+[a-z]", schema, re.I)}
                assert len(targets) == len(selected) and set(selected) <= declared, batch
                path = unquote(urlsplit(url).path).lstrip("/").split("/", 1)[1]
                data = [dict(zip(targets, (r.get(n) for n in selected))) for r in self.parquet_rows[path]]
                self.wh_data.setdefault(key, []).extend(data)
                self.wh_rows[key] = self.wh_rows.get(key, 0) + len(data)
            return
        m = re.match(r"^COPY INTO \[(.+?)\]\.\[(.+?)\]", batch)
        if m:
            k = (item_id, f"{m.group(1)}.{m.group(2)}")
            self.wh_rows[k] = self.wh_rows.get(k, 0) + 7
            return
        m = re.match(r"^CREATE VIEW (\w+)\.(\w+)", batch)
        if m:
            self.sql_objects.setdefault(item_id, set()).add(f"{m.group(1)}/{m.group(2)}")
            return
        raise AssertionError(batch)

    def lro(self):
        op = self.new_id()
        self.ops[op] = True
        return Resp(202, headers={"Location": f"https://api.fabric.microsoft.com/v1/operations/{op}",
                                  "x-ms-operation-id": op, "Retry-After": "1"})

    def fabric(self, method, path, body, params):
        if path == "/admin/tenantsettings":
            geo = getattr(self, "geo_setting", {"enabled": True})
            return Resp(200, {"tenantSettings": [
                {"settingName": "GitIntegrationSyncSwitch", "tenantSettingGroup": "Git integration", "enabled": True,
                 "title": "Users can synchronize workspace items with their Git repositories"},
                {"settingName": "GitIntegrationCrossGeoSwitch", "tenantSettingGroup": "Git integration",
                 "title": "Users can export items to Git repositories in other geographical locations", **geo},
                {"settingName": "Other", "tenantSettingGroup": "Export", "title": "Export to Excel", "enabled": True}]})
        if path == "/capacities":
            return Resp(200, {"value": [{"id": CAP, "region": "UAE North"}]})
        if path == "/admin/workspaces":
            return Resp(200, {"workspaces": []}) if getattr(self, "is_admin", True) else err(403, "Forbidden")
        if path == "/connections" and method == "GET":
            return Resp(200, {"value": getattr(self, "connections", [])})
        if path == "/connections" and method == "POST":
            cred = body["credentialDetails"]["credentials"]
            assert body["connectionDetails"]["type"] == "AzureDevOpsSourceControl" and \
                body["connectionDetails"]["parameters"][0]["value"] == "https://dev.azure.com/org/proj/_git/repo/" and \
                cred["credentialType"] == "ServicePrincipal" and cred["servicePrincipalSecret"] == "abc8Q~" + "s" * 34, body
            c = {"id": "conn-new", "displayName": body["displayName"],
                 "connectionDetails": {"type": "AzureDevOpsSourceControl"}}
            self.connections = getattr(self, "connections", []) + [c]
            return Resp(201, c)
        m = re.match(r"^/connections/([^/]+)$", path)
        if m:
            return Resp(200, {"displayName": "ado-sp", "connectionDetails": {"type": "AzureDevOpsSourceControl"}})
        m = re.match(r"^/operations/([^/]+)(/result)?$", path)
        if m:
            return err(404, "OperationHasNoResult") if m.group(2) else Resp(200, {"status": "Succeeded"})
        m = re.match(r"^/admin/workspaces/([^/]+)/users$", path)
        if m:
            role = self.roles.get(m.group(1), {}).get("me-oid", "Admin")
            return Resp(200, {"accessDetails": [{"principal": {"id": "me-oid", "type": "User", "userDetails": {
                "userPrincipalName": "admin@migration.example"}}, "workspaceAccessDetails": {"workspaceRole": role}}]
                if role else []})
        if path == "/workspaces" and method == "GET":
            return Resp(200, {"value": [{"id": k, "displayName": v["name"]} for k, v in self.ws.items()]})
        m = re.match(r"^/workspaces/([^/]+)(.*)$", path)
        wid, rest = m.group(1), m.group(2)
        if wid not in self.ws:
            return err(404, "WorkspaceNotFound")
        w = self.ws[wid]
        if rest == "" and method == "GET":
            return Resp(200, {"id": wid, "displayName": w["name"], "capacityId": w["capacityId"]})
        if rest == "/items" and method == "GET":
            return Resp(200, {"value": [{k: i[k] for k in ("id", "type", "displayName")} for i in w["items"]]})
        if rest == "/lakehouses" and method == "GET":
            return Resp(200, {"value": [{"id": i["id"], "displayName": i["displayName"],
                                        "properties": {"sqlEndpointProperties": {"id": self.ep(i["id"])}}}
                                       for i in w["items"] if i["type"] == "Lakehouse"]})
        m = re.match(r"^/kqlDatabases/([^/]+)$", rest)
        if m:
            return Resp(200, {"properties": {"parentEventhouseItemId": self.EH, "queryServiceUri": "https://kusto.example",
                                             "databaseName": m.group(1)}})
        m = re.match(r"^/(warehouses|sqlEndpoints)/([^/]+)$", rest)
        if m:
            if m.group(1) == "sqlEndpoints":
                it = self.item(wid, m.group(2))
                return Resp(200, {"id": it["id"], "displayName": it["displayName"], "type": it["type"]})
            return Resp(200, {"properties": {"connectionString": f"srv-{m.group(2)}"}})
        m = re.match(r"^/items/([^/]+)$", rest)
        if m and method == "GET":
            it = self.item(wid, m.group(1))
            return Resp(200, {"id": it["id"], "displayName": it["displayName"], "type": it["type"]}) if it else err(404, "ItemNotFound")
        m = re.match(r"^/lakehouses/([^/]+)$", rest)
        if m:
            props = {"sqlEndpointProperties": {"id": self.ep(m.group(1))}}
            if getattr(self, "lh_schema", ""):
                props["defaultSchema"] = self.lh_schema
            return Resp(200, {"properties": props})
        if rest == "/items" and method == "POST":           # create item (with or without a definition)
            self.created_bodies = getattr(self, "created_bodies", []) + [body]
            if any(i["type"] == body["type"] and i["displayName"] == body["displayName"] for i in w["items"]):
                return err(409, "ItemDisplayNameAlreadyInUse")
            if "definition" in body and body["type"] in getattr(self, "create_no_definition", ()):
                return err(400, "InvalidDefinition")
            parts = (body.get("definition") or {}).get("parts") or []
            assert all(p["path"] != ".platform" for p in parts), parts
            content = base64.b64decode(parts[0]["payload"]).decode() if parts else ""
            iid = self.add_item(wid, body["type"], body["displayName"], content)
            if body["type"] == "Eventhouse":                # Fabric creates the first KQL database with it
                self.add_item(wid, "KQLDatabase", body["displayName"], "auto-created")
            return self.lro() if getattr(self, "create_lro", False) else Resp(201, {"id": iid, **body})
        m = re.match(r"^/items/([^/]+)(/getDefinition|/updateDefinition)?$", rest)
        if m:
            iid, op = m.groups()
            it = self.item(wid, iid)
            if not it:
                return err(404, "ItemNotFound")
            if method == "DELETE":
                self.delete_modes = getattr(self, "delete_modes", []) + [str(params.get("hardDelete", "")).lower()]
                if w["git"]["state"] != "NotConnected":
                    self.deleted_while_connected = True
                w["items"] = [i for i in w["items"] if i["id"] != iid and i.get("parent") != iid]
                return Resp(200)
            if op == "/getDefinition":
                if it["type"] in getattr(self, "no_definition", ()):
                    return err(400, "OperationNotSupportedForItem")
                parts = [{"path": "content.txt", "payload": base64.b64encode(self.defs[iid].encode()).decode()},
                         {"path": ".platform", "payload": base64.b64encode(b"{}").decode()}]
                return Resp(200, {"definition": {"parts": parts}})
            if op == "/updateDefinition":
                assert all(p["path"] != ".platform" for p in body["definition"]["parts"])
                self.defs[iid] = base64.b64decode(body["definition"]["parts"][0]["payload"]).decode()
                self.update_bodies.append(iid)
                return Resp(200)
        g = w["git"]
        if rest == "/git/myGitCredentials":
            if method == "PATCH":
                assert body["source"] == "ConfiguredConnection" and body["connectionId"]
                g["creds"] = body
                return Resp(200, body)
            if g.get("creds_override"):
                return Resp(200, g["creds_override"])
            return Resp(200, g.get("creds") or {"source": getattr(self, "cred_source", "Automatic")})
        if getattr(self, "broken_creds", False) and rest in ("/git/status", "/git/commitToGit", "/git/updateFromGit",
                                                             "/git/disconnect") \
                and (g.get("creds") or {}).get("source") != "ConfiguredConnection":
            return err(401, "GitCredentialsUnauthorized", "The Git provider rejected the credentials of the caller.")
        if rest == "/git/connection":
            if g["state"] == "NotConnected":
                return Resp(200, {"gitConnectionState": "NotConnected"})
            return Resp(200, {"gitConnectionState": g["state"], "gitProviderDetails": {
                "gitProviderType": "AzureDevOps", "organizationName": "org", "projectName": "proj",
                "repositoryName": "repo", "branchName": g["branch"], "directoryName": g["dir"]},
                "gitSyncDetails": {"head": g.get("head")}})
        if rest == "/git/status":
            if g["state"] == "NotConnected":
                return err(400, "WorkspaceNotConnectedToGit")
            return Resp(200, {"workspaceHead": g["head"], "remoteCommitHash": self.refs[g["branch"]],
                              "changes": self.changes(wid)})
        if rest == "/git/commitToGit":
            assert body["mode"] == "All" and body["workspaceHead"] == g["head"]
            base = self.refs[g["branch"]]
            assert g["head"] in (base, None), "fake supports commit only when in sync"
            files = {p: b for p, b in self.commits[base].items() if not p.startswith(g["dir"].rstrip("/") + "/")
                     or "/" not in p[len(g["dir"].rstrip("/")) + 1:]}     # non-item files in the folder stay
            new = self.commit({g["dir"] + "/" + self.path(k)[len(DIR) + 1:]: c for k, c in self.git_view(wid).items()}, base)
            self.commits[new].update(files)
            sha = new
            self.refs[g["branch"]] = sha
            g["head"] = sha
            return self.lro()
        if rest == "/git/disconnect":
            w["git"] = {"state": "NotConnected"}
            return Resp(200)
        if rest == "/git/connect":
            if self.roles.get(wid, {}).get("me-oid", "Admin") != "Admin":
                return err(403, "InsufficientPrivileges", "Caller has insufficient workspace permissions")
            if getattr(self, "cross_geo", False):
                return err(400, "CrossGeographyOperationIsNotAllowed",
                           "The operation is not allowed due to potential cross geo operation.")
            if getattr(self, "fabric_cant_see", False) and "myGitCredentials" not in body:
                return err(404, "GitProviderResourceNotFound", "The requested operation can't be completed because the "
                           "Git provider resource could not be found using the provided Git connection details.")
            if getattr(self, "no_creds", False) and "myGitCredentials" not in body:
                return err(400, "GitCredentialsNotConfigured", "Git credentials are not configured")
            gp = body["gitProviderDetails"]
            d = gp["directoryName"].rstrip("/") + "/"
            if gp["branchName"] in self.refs and d != "/" and not any(
                    x.startswith(d) for x in self.commits[self.refs[gp["branchName"]]]):
                return err(404, "GitProviderResourceNotFound", "The requested operation can't be completed because the "
                           "Git provider resource could not be found using the provided Git connection details.")
            assert g["state"] == "NotConnected" and gp["branchName"] in self.refs
            w["git"] = {"state": "Connected", "branch": gp["branchName"], "dir": gp["directoryName"], "head": None,
                        "creds": body.get("myGitCredentials")}
            return Resp(200)
        if rest == "/git/initializeConnection":
            g["state"] = "ConnectedAndInitialized"
            self.init_bodies = getattr(self, "init_bodies", []) + [body]
            if body["initializationStrategy"] == "PreferWorkspace":
                return Resp(200, {"requiredAction": "CommitToGit" if self.git_view(wid) else "None",
                                  "workspaceHead": None, "remoteCommitHash": self.refs[g["branch"]]})
            assert body == {"initializationStrategy": "PreferRemote"}
            return Resp(200, {"requiredAction": "UpdateFromGit", "workspaceHead": None,
                              "remoteCommitHash": self.refs[g["branch"]]})
        if rest == "/git/updateFromGit":
            self.last_update = body
            if getattr(self, "fail_update", 0):
                self.fail_update -= 1
                return err(400, "WorkspacePreviousOperationInProgress")
            remote = self.tree_view(body["remoteCommitHash"], g["dir"])
            have = {f"{i['type']}|{i['displayName']}" for i in w["items"]}
            if have & set(remote) and not body.get("options", {}).get("allowOverrideItems"):
                return err(400, "PotentialDuplicateDisplayNameAndType", f"{sorted(have & set(remote))}")
            for k, c in remote.items():
                t, n = k.split("|")
                content, lid = c.rsplit("|", 1)
                self.add_item(wid, t, n, content, logical=lid)
            g["head"] = body["remoteCommitHash"]
            return self.lro()
        raise AssertionError((method, path))

    def devops(self, method, path, params, body):
        if getattr(self, "bad_pat", False):
            return Resp(203, raw=b"<html>Sign in</html>", headers={"Content-Type": "text/html; charset=utf-8"})
        if path == "/org/_apis/connectionData":
            return Resp(200, {"authenticatedUser": {"providerDisplayName": "Test user"}})
        if path == "/org/_apis/projects/proj":
            return Resp(200, {"name": "proj"})
        if path == "/org/proj/_apis/git/repositories":
            return Resp(200, {"value": [{"name": "repo"}, {"name": "other"}]})
        if path.startswith("/org/proj/_apis/git/repositories/") and not path.startswith("/org/proj/_apis/git/repositories/repo"):
            return err(404, "GitRepositoryNotFound")
        assert path.startswith("/org/proj/_apis/git/repositories/repo"), path
        rest = path[len("/org/proj/_apis/git/repositories/repo"):]
        if rest == "" and method == "GET":
            return Resp(200, {"defaultBranch": "refs/heads/main"})
        if rest == "/refs" and method == "GET" and params["filter"].startswith("tags/"):
            t = params["filter"][len("tags/"):]
            return Resp(200, {"value": [{"name": f"refs/tags/{t}"}] if t in self.tags else []})
        if rest == "/annotatedtags" and method == "POST":
            self.tags[body["name"]] = body["taggedObject"]["objectId"]
            return Resp(201, {"name": body["name"]})
        if rest == "/refs" and method == "GET" and params["filter"] == "heads/main" and getattr(self, "move_main", False):
            self.move_main = False
            files = dict(self.commits[self.refs["main"]])
            files[f"{DIR}/NB.Notebook/content.txt"] = b"changed by someone else"
            sha = hashlib.sha1(b"moved" + json.dumps(sorted(files)).encode()).hexdigest()
            self.commits[sha] = files
            self.refs["main"] = sha
        if rest == "/refs" and method == "GET":
            b = params["filter"][len("heads/"):]
            refs = [{"name": f"refs/heads/{n}", "objectId": s, "isLocked": n in self.locked}
                    for n, s in self.refs.items() if n.startswith(b)]
            return Resp(200, {"value": refs})
        if rest == "/refs" and method == "POST":
            r = body[0]
            n = r["name"][len("refs/heads/"):]
            if r["newObjectId"] == "0" * 40:
                assert self.refs.get(n) == r["oldObjectId"] and n not in self.locked
                del self.refs[n]
                return Resp(200, {"value": [{"success": True}]})
            if n in self.refs:
                return Resp(200, {"value": [{"success": False, "updateStatus": "staleOldObjectId"}]})
            self.refs[n] = r["newObjectId"]
            return Resp(200, {"value": [{"success": True}]})
        if rest == "/refs" and method == "PATCH":
            n = params["filter"][len("heads/"):]
            (self.locked.add if body["isLocked"] else self.locked.discard)(n)
            return Resp(200, {})
        if rest == "/items":
            files = self.commits[params["versionDescriptor.version"]]
            if "scopePath" in params:
                sc = params["scopePath"].rstrip("/") + "/"
                hit = [{"path": p, "gitObjectType": "blob", "objectId": hashlib.sha1(files[p]).hexdigest()}
                       for p in files if p.startswith(sc)]
                return Resp(200, {"value": hit}) if hit else err(404, "TF401174")
            if params["path"] not in files:
                return err(404, "GitItemNotFoundException")
            return Resp(200, raw=files[params["path"]], headers={"Content-Type": "application/octet-stream"})
        if rest == "/pushes":
            ru = body["refUpdates"][0]
            n = ru["name"][len("refs/heads/"):]
            assert self.refs[n] == ru["oldObjectId"] and n not in self.locked
            files = dict(self.commits[ru["oldObjectId"]])
            for ch in body["commits"][0]["changes"]:
                if ch["changeType"] == "add":
                    files[ch["item"]["path"]] = ch["newContent"]["content"].encode()
                    continue
                assert ch["changeType"] == "delete"
                del files[ch["item"]["path"]]
            sha = hashlib.sha1(json.dumps(sorted(files)).encode() + b"push").hexdigest()
            self.commits[sha] = files
            self.refs[n] = sha
            return Resp(201, {"commits": [{"commitId": sha}]})
        raise AssertionError((method, path))


def run(world, notebook, settings=""):
    nb = json.load(open(f"{OUT}/{notebook}", encoding="utf-8"))
    def connect_sql(item_id, ws_id, item_type):
        if item_type == "SQLEndpoint":
            raise ValueError("Artifact type SQLEndpoint is not supported to connect.")
        if item_type == "Lakehouse":
            assert world.item(ws_id, item_id)["type"] == "Lakehouse"
            return world.sql_conn(world.ep(item_id))
        assert item_type == "Warehouse" and world.item(ws_id, item_id)["type"] == "Warehouse"
        return world.sql_conn(item_id)

    nu = types.SimpleNamespace(credentials=types.SimpleNamespace(getToken=lambda a: world.jwt(),
                                                                 getSecret=lambda v, n: getattr(world, "secrets", {}).get(
                                                                     n, getattr(world, "pat", "p" * 52))),
                               runtime=types.SimpleNamespace(context={"currentWorkspaceId": getattr(world, "here_ws", "")}),
                               data=types.SimpleNamespace(connect_to_artifact=connect_sql),
                               fs=types.SimpleNamespace(fastcp=world.fastcp))
    sys.modules["notebookutils"] = nu

    requests.Session.request = lambda self, method, url, **kw: world.request(method, url, **kw)
    import time as _t
    real = _t.sleep
    _t.sleep = lambda s: None
    ns = {"__name__": "__main__", "display": lambda df: print(df.to_string())}
    base = f'''
ADLS_ACCOUNT = "acct"
KEY_VAULT_URL = "https://kv.vault.azure.net/"
ADO_PAT_SECRET_NAME = "pat"
ADO_ORGANIZATION = "org"
ADO_PROJECT = "proj"
ADO_REPOSITORY = "repo"
SECONDARY_BACKUP_RUN_ID = "{RUN}"
FREEZE_STARTED_UTC = "2026-11-20T18:00:00Z"
WORKSPACE_IDS = ["{W1}"]
GIT_LINK_AUTH = "my_account"
'''
    try:
        for i, cell in enumerate(c for c in nb["cells"] if c["cell_type"] == "code"):
            exec("".join(cell["source"]), ns)
            if i == 0:
                exec(base + settings, ns)
    finally:
        _t.sleep = real
    return ns
