from odoo import api, fields, models, _
from odoo.exceptions import UserError
import requests

PARAM_BASE = "utel_integration.base_url"
PARAM_TOKEN = "utel_integration.token"
PARAM_PER_PAGE = "utel_integration.per_page"
PARAM_AUTO_DAYS = "utel_integration.auto_days"


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # plain transient fields (we'll persist them ourselves)
    utel_base_url = fields.Char(string="Utel Base URL", default="https://api.utc118.utel.uz")
    utel_token = fields.Char(string="Utel API Token", help="Paste FULL value including 'Bearer ' prefix.")
    utel_per_page = fields.Integer(string="Per page", default=50)
    utel_auto_days = fields.Integer(string="Auto-sync last N days", default=3)

    # ---------- LOW-LEVEL PERSISTENCE ----------
    def _write_params(self):
        """Persist current wizard values to ir.config_parameter."""
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param(PARAM_BASE, (self.utel_base_url or "").strip())
        ICP.set_param(PARAM_TOKEN, (self.utel_token or "").strip())
        ICP.set_param(PARAM_PER_PAGE, str(self.utel_per_page or 50))
        ICP.set_param(PARAM_AUTO_DAYS, str(self.utel_auto_days or 3))

    @api.model
    def get_values(self):
        """Load params when the wizard opens."""
        res = super().get_values()
        ICP = self.env["ir.config_parameter"].sudo()
        res.update(
            utel_base_url=ICP.get_param(PARAM_BASE, default="https://api.utc118.utel.uz"),
            utel_token=ICP.get_param(PARAM_TOKEN, default=""),
            utel_per_page=int(ICP.get_param(PARAM_PER_PAGE, default="50")),
            utel_auto_days=int(ICP.get_param(PARAM_AUTO_DAYS, default="3")),
        )
        return res

    def set_values(self):
        """Called by the standard Save button in Settings."""
        super().set_values()
        self._write_params()

    # ---------- BUTTONS ----------
    def action_utel_save_params(self):
        """Explicit 'Save parameters' button — works without the big Settings Save."""
        self.ensure_one()
        self._write_params()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Saved"),
                "message": _("Utel parameters saved to System Parameters."),
                "type": "success",
            },
        }

    def action_utel_test_connection(self):
        """Save current fields if needed, then call the API and report status."""
        self.ensure_one()
        # Always persist what is on the screen, so user doesn't have to press the big Save.
        self._write_params()

        ICP = self.env["ir.config_parameter"].sudo()
        base = (ICP.get_param(PARAM_BASE) or "").rstrip("/")
        token = (ICP.get_param(PARAM_TOKEN) or "").strip()
        if not base or not token:
            raise UserError(_("Please fill Utel Base URL and Token, then click 'Save parameters' or 'Test connection' again."))

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
            "params": {
                "title": _("Utel Test"),
                "message": msg,
                "type": "success" if status == 200 else "warning",
                "sticky": True,
            },
        }

    def action_utel_manual_sync(self):
        """Manual import (first 5 pages). It will also persist the current values first."""
        self.ensure_one()
        self._write_params()
        return self.env["utel.call"].action_sync_all_pages(page_limit=5)
