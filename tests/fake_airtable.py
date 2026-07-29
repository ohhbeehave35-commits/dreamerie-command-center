"""An in-memory Airtable that enforces the ONE rule that broke Add User.

WHY THIS EXISTS
The original Add User test monkeypatched users.add_user -- the exact function
that was broken -- so it asserted the route returned success while stubbing
away the save that was failing. It could not fail. A test that mocks the thing
under test proves nothing.

The honest way to test a function that talks to Airtable is to fake ONLY the
HTTP layer and run the REAL function against it, with the fake enforcing the
behaviours that actually bite:

  1. A record-create naming a column the table doesn't have returns 422 with
     "Unknown field name" -- and does NOT add the column. This is the entire
     Add User bug: a table created by an older deploy, missing a field the code
     now writes, rejecting every insert forever.
  2. POST .../tables/<id>/fields adds a column (the migration fix), after which
     the same insert succeeds.
  3. Tables and records otherwise behave: create, list, patch, delete.

So a missing migration REPRODUCES here -- the real add_user against an
old-shaped table returns (False, "...422...Unknown field...") -- and the fix
(migrate on the existing-table branch) makes it pass. No mock of add_user, no
mock of _ensure_users_table. The bug is caught by the same mechanism that
would catch it in production.
"""

import json
import re

import httpx


class FakeBase:
    """State for one Airtable base."""

    def __init__(self):
        # name -> {"id": str, "fields": [names], "records": {recid: {fields}}}
        self.tables = {}
        self._tid = 0
        self._rid = 0

    def seed_table(self, name, fields):
        """Create a table with a FIXED set of columns -- use this to simulate a
        table an OLDER deploy created, missing a column the code now writes."""
        self._tid += 1
        tid = f"tbl{self._tid:06d}"
        self.tables[name.lower()] = {"id": tid, "name": name,
                                     "fields": list(fields), "records": {}}
        return tid


def make_transport(base: FakeBase):
    """An httpx.MockTransport that speaks just enough of the Airtable API."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        body = {}
        if request.content:
            try:
                body = json.loads(request.content)
            except Exception:
                body = {}

        # --- list tables (meta) ---
        if method == "GET" and path.endswith("/meta/bases") is False and "/meta/bases/" in path and path.endswith("/tables"):
            return _json(200, {"tables": [
                {"id": t["id"], "name": t["name"],
                 "fields": [{"name": n} for n in t["fields"]]}
                for t in base.tables.values()]})

        # --- create a table ---
        if method == "POST" and "/meta/bases/" in path and path.endswith("/tables"):
            name = body.get("name", "Table")
            fields = [f["name"] for f in body.get("fields", [])]
            tid = base.seed_table(name, fields)
            return _json(200, {"id": tid, "name": name})

        # --- add a field to a table (THE migration) ---
        m = re.search(r"/meta/bases/[^/]+/tables/([^/]+)/fields$", path)
        if method == "POST" and m:
            tid = m.group(1)
            for t in base.tables.values():
                if t["id"] == tid:
                    fname = body.get("name")
                    if fname and fname not in t["fields"]:
                        t["fields"].append(fname)
                    return _json(200, {"id": "fld" + fname})
            return _json(404, {"error": "table not found"})

        # --- record endpoints: /v0/<base>/<tid>[/<recid>] ---
        rec = re.search(r"/v0/[^/]+/([^/]+)(?:/([^/]+))?$", path)
        if rec and "/meta/" not in path:
            tid, recid = rec.group(1), rec.group(2)
            table = next((t for t in base.tables.values() if t["id"] == tid), None)
            if table is None:
                return _json(404, {"error": {"type": "TABLE_NOT_FOUND"}})

            if method == "POST":
                fields = body.get("fields", {})
                unknown = [k for k in fields if k not in table["fields"]]
                if unknown:
                    # THE bug. Real Airtable 422s and does NOT add the column.
                    return _json(422, {"error": {
                        "type": "UNKNOWN_FIELD_NAME",
                        "message": f'Unknown field name: "{unknown[0]}"'}})
                base._rid += 1
                rid = f"rec{base._rid:06d}"
                table["records"][rid] = dict(fields)
                return _json(200, {"id": rid, "fields": fields, "createdTime": "2026-07-29T00:00:00Z"})

            if method == "GET":
                return _json(200, {"records": [
                    {"id": rid, "fields": f, "createdTime": "2026-07-29T00:00:00Z"}
                    for rid, f in table["records"].items()]})

            if method == "PATCH":
                if recid in table["records"]:
                    fields = body.get("fields", {})
                    unknown = [k for k in fields if k not in table["fields"]]
                    if unknown:
                        return _json(422, {"error": {
                            "type": "UNKNOWN_FIELD_NAME",
                            "message": f'Unknown field name: "{unknown[0]}"'}})
                    table["records"][recid].update(fields)
                    return _json(200, {"id": recid, "fields": table["records"][recid]})
                return _json(404, {"error": "record not found"})

            if method == "DELETE":
                table["records"].pop(recid, None)
                return _json(200, {"deleted": True, "id": recid})

        return _json(404, {"error": {"type": "NOT_FOUND", "path": path}})

    return httpx.MockTransport(handler)


def _json(status, payload) -> httpx.Response:
    return httpx.Response(status, json=payload)


def install(monkeypatch, module, base: FakeBase):
    """Point `module`'s httpx.Client at the fake base.

    The functions under test build their own httpx.Client(timeout=...), so we
    replace that constructor with one carrying the mock transport. The real
    request-building, URL construction, status handling and JSON parsing all
    run unchanged -- only the socket is faked.
    """
    transport = make_transport(base)
    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=transport, **{k: v for k, v in kwargs.items()
                                                   if k in ("timeout", "headers")})

    monkeypatch.setattr(module.httpx, "Client", client_factory)
