# -*- coding: utf-8 -*-
import logging
import json
from odoo import http, _
from odoo.http import request
from werkzeug.wrappers.response import Response

_logger = logging.getLogger(__name__)

PARAM_WEBHOOK_TOKEN = "utel_integration.webhook_token"

def _cfg_token():
    return (request.env["ir.config_parameter"].sudo().get_param(PARAM_WEBHOOK_TOKEN) or "").strip()

def _auth_ok(req):
    # Accept: "<token>" or "Bearer <token>" (case-insensitive for prefix)
    hdr = req.headers.get("Authorization", "").strip()
    tok = _cfg_token()
    if not tok:
        return False
    if hdr == tok:
        return True
    if hdr.lower().startswith("bearer ") and hdr[7:].strip() == tok:
        return True
    return False

def _read_body_any(req):
    """
    Return Python object from JSON or form-encoded or raw text JSON.
    Always returns Python object or {}.
    """
    # 1) JSON if present
    try:
        if req.is_json:
            return req.get_json(silent=True) or {}
    except Exception:
        pass
    # 2) Raw body → try JSON
    try:
        raw = req.get_data(cache=False, as_text=True)  # str
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    # 3) Form → dict-ish
    if req.form:
        # Some providers send a 'payload' field containing JSON
        if "payload" in req.form:
            try:
                return json.loads(req.form["payload"])
            except Exception:
                return {"payload": req.form.get("payload")}
        return dict(req.form)
    return {}

class UtelController(http.Controller):

    @http.route("/utel/webhooks/call", type="http", auth="none", methods=["POST"], csrf=False)
    def utel_webhook(self):
        req = request.httprequest

        if not _auth_ok(req):
            _logger.warning("UTEL webhook unauthorized. Headers=%s", dict(req.headers))
            return Response(json.dumps({"ok": False, "error": "unauthorized"}), 401, mimetype="application/json")

        body = _read_body_any(req)
        # Normalize: allow single dict OR {"data":[...]} OR list
        if isinstance(body, list):
            records = body
        elif isinstance(body, dict):
            records = body.get("data") if isinstance(body.get("data"), list) else [body]
        else:
            records = []

        _logger.info("UTEL webhook received %s record(s)", len(records))

        Model = request.env["utel.call"].sudo()
        created = updated = 0
        for rec in records or []:
            try:
                vals = Model._to_vals(rec)
                utel_id = vals.get("utel_id")
                if not utel_id:
                    continue
                existing = Model.search([
                    ("utel_id", "=", utel_id),
                    ("company_id", "=", request.env.company.id)
                ], limit=1)
                if existing:
                    existing.write(vals); updated += 1
                else:
                    Model.create(vals); created += 1
            except Exception as e:
                _logger.exception("UTEL webhook upsert error: %s (rec=%s)", e, rec)

        out = {"ok": True, "created": created, "updated": updated}
        _logger.info("UTEL webhook result: %s", out)
        return Response(json.dumps(out), 200, mimetype="application/json")
