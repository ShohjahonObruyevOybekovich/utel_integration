# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError

class ResUsers(models.Model):
    _inherit = "res.users"
    utel_last_seen_dt = fields.Datetime(string="Utel Calls Last Seen")

class UtelCall(models.Model):
    _inherit = "utel.call"

    def action_open_play(self):
        self.ensure_one()
        if not (self.play_url or self.download_url):
            raise UserError(_("No play URL on this call."))
        return {"type": "ir.actions.act_url", "url": f"/utel/player/{self.id}", "target": "new"}

    def action_open_download(self):
        self.ensure_one()
        if not self.download_url:
            raise UserError(_("No download URL on this call."))
        return {"type": "ir.actions.act_url", "url": self.download_url, "target": "new"}

    def action_delete_call(self):
        self.check_access_rights('unlink')
        self.check_access_rule('unlink')
        self.unlink()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("Deleted"), "message": _("Call deleted."), "type": "success"},
        }

    def action_delete_selected(self):
        self.check_access_rights('unlink')
        self.check_access_rule('unlink')
        count = len(self)
        self.unlink()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("Deleted"), "message": _("%s call(s) deleted.") % count, "type": "success"},
        }

    @api.model
    def action_open_with_toast(self):
        user = self.env.user.sudo()
        domain = [("create_date", ">", user.utel_last_seen_dt)] if user.utel_last_seen_dt else []
        new_count = self.sudo().search_count(domain)

        if new_count and user.partner_id:
            Bus = self.env["bus.bus"].sudo()
            dbname = self._cr.dbname
            channel = (dbname, "res.partner", user.partner_id.id)
            payload = {
                "type": "simple_notification",
                "title": _("Utel"),
                "message": _("%(n)s new calls since your last visit.", n=new_count),
                "sticky": False,
            }

            sent = False
            for meth in ("_sendmany", "sendmany"):
                fn = getattr(Bus, meth, None)
                if callable(fn):
                    try:
                        fn([(channel, payload)])
                        sent = True
                        break
                    except Exception:
                        pass
            if not sent:
                for meth in ("_sendone", "sendone"):
                    fn = getattr(Bus, meth, None)
                    if callable(fn):
                        try:
                            fn(channel, payload)
                            break
                        except Exception:
                            continue

        user.utel_last_seen_dt = fields.Datetime.now()
        return self.env.ref("utel_integration.action_utel_calls").sudo().read()[0]
