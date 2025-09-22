# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError
import requests

PARAM_BASE = "utel_integration.base_url"
PARAM_TOKEN = "utel_integration.token"
PARAM_PER_PAGE = "utel_integration.per_page"
PARAM_AUTO_DAYS = "utel_integration.auto_days"

# NEW
PARAM_DIDS = "utel_integration.did_numbers"
PARAM_NOTIFY_ENABLED = "utel_integration.notify_enabled"
PARAM_NOTIFY_GROUP_XMLID = "utel_integration.notify_group_xmlid"

class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    utel_base_url = fields.Char(string="Utel Base URL", default="https://api.utc118.utel.uz")
    utel_token = fields.Char(string="Utel API Token", help="Paste FULL value including 'Bearer ' prefix.")
    utel_per_page = fields.Integer(string="Per page", default=50)
    utel_auto_days = fields.Integer(string="Auto-sync last N days", default=3)

    utel_did_numbers = fields.Char(
        string="Your DID numbers",
        help="Comma-separated list; digits only or with +/spaces. Example: 998339993099, 998123456789",
    )

    utel_notify_enabled = fields.Boolean(string="Enable toast notifications", default=True)
    utel_notify_group_xmlid = fields.Char(
        string="Notify this group (XML ID)",
        help="Optional XML ID (e.g. base.group_user). Leave empty to notify all internal users of the current company.",
    )

    def _write_params(self):
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param(PARAM_BASE, (self.utel_base_url or "").strip())
        ICP.set_param(PARAM_TOKEN, (self.utel_token or "").strip())
        ICP.set_param(PARAM_PER_PAGE, str(self.utel_per_page or 50))
        ICP.set_param(PARAM_AUTO_DAYS, str(self.utel_auto_days or 3))
        ICP.set_param(PARAM_DIDS, (self.utel_did_numbers or "").strip())
        ICP.set_param(PARAM_NOTIFY_ENABLED, "1" if self.utel_notify_enabled else "0")
        ICP.set_param(PARAM_NOTIFY_GROUP_XMLID, (self.utel_notify_group_xmlid or "").strip())

    @api.model
    def get_values(self):
        res = super().get_values()
        ICP = self.env["ir.config_parameter"].sudo()
        res.update(
            utel_base_url=ICP.get_param(PARAM_BASE, default="https://api.utc118.utel.uz"),
            utel_token=ICP.get_param(PARAM_TOKEN, default=""),
            utel_per_page=int(ICP.get_param(PARAM_PER_PAGE, default="50")),
            utel_auto_days=int(ICP.get_param(PARAM_AUTO_DAYS, default="3")),
            utel_did_numbers=ICP.get_param(PARAM_DIDS, default=""),
            utel_notify_enabled=(ICP.get_param(PARAM_NOTIFY_ENABLED, default="1") not in {"0", "false", "False", ""}),
            utel_notify_group_xmlid=ICP.get_param(PARAM_NOTIFY_GROUP_XMLID, default=""),
        )
        return res

    def set_values(self):
        super().set_values()
        self._write_params()

    def action_utel_save_params(self):
        self.ensure_one()
        self._write_params()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("Saved"), "message": _("Utel parameters saved."), "type": "success"},
        }

    def action_utel_test_connection(self):
        self.ensure_one()
        self._write_params()

        ICP = self.env["ir.config_parameter"].sudo()
        base = (ICP.get_param(PARAM_BASE) or "").rstrip("/")
        token = (ICP.get_param(PARAM_TOKEN) or "").strip()
        if not base or not token:
            raise UserError(_("Please fill Utel Base URL and Token, then try again."))

        url = f"{base}/api/v1/call-history"
        params = {"per_page": 1, "page": 1, "sort": "-date_time"}
        headers = {"Accept": "application/json", "Authorization": token}

        try:
            r = requests.get(url, headers=headers, params=params, timeout=20)
        except Exception as e:
            raise UserError(_("Network error while calling Utel: %s") % e)

        status = r.status_code
        msg = _("HTTP %s") % status
        try:
            js = r.json()
            if isinstance(js, dict):
                cnt = len(js.get("data", []))
                msg += _(" • data items: %s") % cnt
                if "meta" in js and isinstance(js["meta"], dict):
                    msg += _(" • page %s/%s") % (js["meta"].get("current_page"), js["meta"].get("last_page"))
        except Exception:
            msg += _(" • Response: %s") % (r.text[:200])

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("Utel Test"), "message": msg,
                       "type": "success" if status == 200 else "warning", "sticky": True},
        }

    def action_utel_manual_sync(self):
        self.ensure_one()
        self._write_params()
        return self.env["utel.call"].action_sync_all_pages(page_limit=5)
