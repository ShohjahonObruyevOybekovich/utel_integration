# -*- coding: utf-8 -*-
import logging
from datetime import datetime, timedelta
import requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

PARAM_BASE = "utel_integration.base_url"
PARAM_TOKEN = "utel_integration.token"
PARAM_PER_PAGE = "utel_integration.per_page"
PARAM_AUTO_DAYS = "utel_integration.auto_days"


def _parse_hms(val):
    """Convert 'HH:MM' or 'HH:MM:SS' to seconds (int). Accepts '00:06' etc."""
    if not val:
        return 0
    parts = f"{val}".split(":")
    try:
        if len(parts) == 2:
            h, m = 0, int(parts[0])
            s = int(parts[1])
        elif len(parts) == 3:
            h, m, s = [int(x) for x in parts]
        else:
            return int(val)
        return h * 3600 + m * 60 + s
    except Exception:
        return 0

def _as_text(v):
    if v is None:
        return ""
    if isinstance(v, dict):
        for k in ("code", "key", "value", "name", "label", "status", "type", "url"):
            if k in v and v[k]:
                return str(v[k])
        return ""
    if isinstance(v, (list, tuple)) and v:
        return _as_text(v[0])
    return str(v)


class UtelCall(models.Model):
    _name = "utel.call"
    _description = "Utel Call"
    _order = "date_time desc, id desc"
    _rec_name = "src"

    # identifiers
    utel_id = fields.Char(index=True, readonly=True)
    company_id = fields.Many2one("res.company", default=lambda s: s.env.company, index=True)

    # main info
    date_time = fields.Datetime(string="Date Time", index=True)
    type = fields.Selection(
        [
            ("in", "Incoming"),
            ("out", "Outgoing"),
            ("missed", "Missed"),
            ("other", "Other"),
        ],
        default="other",
        index=True,
    )
    src = fields.Char(string="From", index=True)
    dst = fields.Char(string="To", index=True)
    external_number = fields.Char(index=True)

    # durations
    talk_time = fields.Integer(string="Talk Time (s)")
    ring_time = fields.Integer(string="Ring Time (s)")

    status = fields.Char(string="Status")
    play_url = fields.Char(string="Play URL")
    download_url = fields.Char(string="Download URL")
    has_recording = fields.Boolean(
        string="Has Recording",
        default=False,
        index=True,
    )

    note = fields.Text()

    _sql_constraints = [
        ("utel_unique", "unique(utel_id, company_id)", "Utel call already imported.")
    ]

    # ---------------------- SYNC CORE ----------------------

    def action_open_play(self):
        self.ensure_one()
        if not (self.play_url or self.download_url):
            raise UserError(_("No play URL on this call."))
        return {
            "type": "ir.actions.act_url",
            "url": f"/utel/player/{self.id}",
            "target": "new",  # opens a tiny tab with the compact player
        }

    def action_open_download(self):
        self.ensure_one()
        if not self.download_url:
            raise UserError(_("No download URL on this call."))
        return {
            "type": "ir.actions.act_url",
            "url": self.download_url,
            "target": "new",
        }


    def _get_conn(self):
        ICP = self.env["ir.config_parameter"].sudo()
        base = (ICP.get_param(PARAM_BASE) or "").rstrip("/")
        token = (ICP.get_param(PARAM_TOKEN) or "").strip()
        per_page = int(ICP.get_param(PARAM_PER_PAGE, default="50") or 50)
        if not base or not token:
            raise UserError(_("Please set Utel Base URL and API Token in Utel → Settings."))
        return base, token, per_page

    def _fetch_page(self, base, token, page=1, per_page=50, date_from=None, date_to=None):
        url = f"{base}/api/v1/call-history"
        params = {
            "per_page": per_page,
            "page": page,
            "sort": "-date_time",
            # IMPORTANT: Utel requires full datetime format "Y-m-d H:i:s"
            "filter[from]": date_from or "",
            "filter[to]": date_to or "",
            "filter[type]": "",
            "filter[status]": "",
            "filter[src]": "",
            "filter[dst]": "",
            "filter[external_number]": "",
        }
        headers = {"Accept": "application/json", "Authorization": token}
        r = requests.get(url, headers=headers, params=params, timeout=30)
        if r.status_code != 200:
            raise UserError(_("Utel API error %s: %s") % (r.status_code, r.text[:300]))
        try:
            return r.json()
        except Exception as e:
            raise UserError(_("Failed to parse Utel response: %s") % e)

    def _to_vals(self, rec):
        # 1) datetime
        dt_raw = rec.get("date_time") or rec.get("datetime") or rec.get("time") or rec.get("started_at")
        try:
            dt = fields.Datetime.to_datetime(_as_text(dt_raw))
        except Exception:
            dt = False

        # 2) durations
        talk_raw = rec.get("talk_time") or rec.get("conversation") or rec.get("duration")
        ring_raw = rec.get("ring_time") or rec.get("ringing") or rec.get("wait_time")
        talk_txt = _as_text(talk_raw)
        ring_txt = _as_text(ring_raw)
        talk_sec = _parse_hms(talk_txt) if ":" in talk_txt else int(talk_txt or 0)
        ring_sec = _parse_hms(ring_txt) if ":" in ring_txt else int(ring_txt or 0)

        # 3) type/status
        rtype_txt = _as_text(rec.get("type")).lower()
        if rtype_txt in ("incoming", "in"):
            rtype = "in"
        elif rtype_txt in ("outgoing", "out"):
            rtype = "out"
        elif rtype_txt in ("missed", "miss", "noanswer", "not answered"):
            rtype = "missed"
        else:
            rtype = "other"
        status_txt = _as_text(rec.get("status"))

        # 4) recording links
        # UTEL shows 'recorded_file_url' in your trace -> use it for play and/or download
        play = (
                rec.get("play_url")
                or rec.get("listen_url")
                or rec.get("record_url")
                or rec.get("audio_url")
                or rec.get("stream_url")
                or rec.get("recorded_file_url")
        )
        download = (
                rec.get("download_url")
                or rec.get("record_download_url")
                or rec.get("file_url")
                or rec.get("record_file")
                or rec.get("record_path")
                or rec.get("recorded_file_url")
        )
        play = rec.get("recorded_file_url") or rec.get("play_url") or ""
        download = rec.get("download_url") or ""

        return {
            "utel_id": str(rec.get("id") or ""),
            "date_time": dt,
            "type": rtype,
            "src": _as_text(rec.get("src")) or "",
            "dst": _as_text(rec.get("dst")) or "",
            "external_number": _as_text(rec.get("external_number")) or "",
            "talk_time": talk_sec,
            "ring_time": ring_sec,
            "status": status_txt,
            "play_url": play,
            "download_url": download,
            "has_recording": bool(play or download),
        }

    # public entry from settings button

    @api.model
    def action_sync_all_pages(self, page_limit=5):
        base, token, per_page = self._get_conn()
        created = updated = 0

        ICP = self.env["ir.config_parameter"].sudo()
        days = int(ICP.get_param(PARAM_AUTO_DAYS, default="3") or 3)

        # Use UTC (or server time) and format as "Y-m-d H:i:s"
        now = datetime.utcnow()
        date_to_dt = now
        date_from_dt = now - timedelta(days=days)
        fmt = "%Y-%m-%d %H:%M:%S"
        date_from_str = date_from_dt.strftime(fmt)
        date_to_str = date_to_dt.strftime(fmt)

        for page in range(1, int(page_limit) + 1):
            payload = self._fetch_page(
                base, token,
                page=page, per_page=per_page,
                date_from=date_from_str, date_to=date_to_str
            )
            data = payload.get("data") or []
            if not data:
                break

            for rec in data:
                vals = self._to_vals(rec)
                if not vals.get("utel_id"):
                    continue
                existing = self.search([
                    ("utel_id", "=", vals["utel_id"]),
                    ("company_id", "=", self.env.company.id)
                ], limit=1)
                if existing:
                    existing.write(vals)
                    updated += 1
                else:
                    self.create(vals)
                    created += 1

            if len(data) < per_page:
                break

        msg = _("Imported from Utel • created: %(c)s • updated: %(u)s") % {"c": created, "u": updated}
        _logger.info(msg)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("Utel Sync"), "message": msg, "type": "success", "sticky": True},
        }
    # scheduled job
    @api.model
    def cron_sync_recent(self):
        try:
            res = self.action_sync_all_pages(page_limit=2)  # quick
            _logger.info("Utel cron executed.")
            return res
        except Exception as e:
            _logger.exception("Utel cron failed: %s", e)
            return False

    player_html = fields.Html(
        string="Player",
        compute="_compute_player_html",
        sanitize=False,
    )
    # RIGHT
    @api.depends('has_recording', 'play_url')
    def _compute_player_html(self):
        for rec in self:
            if rec.has_recording:
                rec.player_html = (
                    f'<audio controls preload="none" style="width:220px;height:28px;">'
                    f'<source src="/utel/stream/{rec.id}" type="audio/mpeg"/>'
                    f'</audio>'
                )
            else:
                rec.player_html = ''

    # --- Add these fields inside class UtelCall(models.Model): ---
    talk_time_display = fields.Char(
        string="Talk Time",
        compute="_compute_time_display",
        store=False,
    )
    ring_time_display = fields.Char(
        string="Ring Time",
        compute="_compute_time_display",
        store=False,
    )

    @api.depends("talk_time", "ring_time")
    def _compute_time_display(self):
        for rec in self:
            rec.talk_time_display = self._format_seconds(rec.talk_time)
            rec.ring_time_display = self._format_seconds(rec.ring_time)

    def _format_seconds(self, seconds: int) -> str:
        """Return 68 -> '1m 8s', 3665 -> '1h 1m 5s', 7 -> '7s'."""
        try:
            total = int(seconds or 0)
        except Exception:
            total = 0
        if total <= 0:
            return "0s"
        m, s = divmod(total, 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}h {m}m {s}s"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"