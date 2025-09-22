# -*- coding: utf-8 -*-
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# System parameters
PARAM_BASE = "utel_integration.base_url"
PARAM_TOKEN = "utel_integration.token"
PARAM_PER_PAGE = "utel_integration.per_page"
PARAM_AUTO_DAYS = "utel_integration.auto_days"
PARAM_TZ = "utel_integration.tz"  # e.g. Asia/Tashkent
PARAM_INTERNAL_EXT = "utel_integration.internal_extensions"  # "101,102,103"

# Notifications
PARAM_NOTIFY_ENABLED = "utel_integration.notify_enabled"          # "1"/"0"
PARAM_NOTIFY_GROUP_XMLID = "utel_integration.notify_group_xmlid"  # e.g. "base.group_user"
PARAM_DIDS = "utel_integration.did_numbers"                       # comma-separated DIDs

# -------------------- helpers --------------------
def _parse_hms(val: str) -> int:
    if not val:
        return 0
    s = f"{val}"
    parts = s.split(":")
    try:
        if len(parts) == 2:
            h, m, s = 0, int(parts[0]), int(parts[1])
        elif len(parts) == 3:
            h, m, s = [int(x) for x in parts]
        else:
            return int(s)
        return h * 3600 + m * 60 + s
    except Exception:
        return 0

def _as_text(v):
    if v is None:
        return ""
    if isinstance(v, dict):
        for k in ("code", "key", "value", "name", "label", "status", "type", "url"):
            if v.get(k):
                return str(v[k])
        return ""
    if isinstance(v, (list, tuple)) and v:
        return _as_text(v[0])
    return str(v)

def _digits_only(num):
    return "".join(ch for ch in str(num or "") if ch.isdigit())

def _uz_digits(d):
    s = _digits_only(d)
    if len(s) == 9:
        return "998" + s
    return s

def _format_uz_pretty(digits):
    s = _digits_only(digits)
    if len(s) == 12 and s.startswith("998"):
        n = s[3:]
        return f"+998 {n[0:2]} {n[2:5]} {n[5:7]} {n[7:9]}"
    return f"+{s}" if s else ""

# -------------------- model --------------------
class UtelCall(models.Model):
    _name = "utel.call"
    _description = "Utel Call"
    _order = "date_time desc, id desc"
    _rec_name = "src"

    # identifiers
    utel_id = fields.Char(index=True, readonly=True)
    company_id = fields.Many2one(
        "res.company", default=lambda s: s.env.company, index=True, required=True
    )

    # main info
    date_time = fields.Datetime(string="Date Time", index=True)
    type = fields.Selection(
        [("in", "Kiruvchi"), ("out", "Chiquvchi"), ("missed", "O'tkazib yuborilgan"), ("other", "Boshqa turdagi")],
        default="other",
        index=True,
    )
    src = fields.Char(string="From", index=True)
    dst = fields.Char(string="To", index=True)
    external_number = fields.Char(string="External Number", index=True)

    # normalized digits
    src_norm = fields.Char(index=True, compute="_compute_normals", store=True, readonly=True)
    dst_norm = fields.Char(index=True, compute="_compute_normals", store=True, readonly=True)
    external_number_norm = fields.Char(index=True, compute="_compute_normals", store=True, readonly=True)

    # linked contact
    partner_id = fields.Many2one("res.partner", index=True, ondelete="set null")

    # durations
    talk_time = fields.Integer(string="Talk Time (s)")
    ring_time = fields.Integer(string="Ring Time (s)")

    # misc / recording
    status = fields.Char(string="Status")
    play_url = fields.Char(string="Play URL")
    download_url = fields.Char(string="Download URL")
    has_recording = fields.Boolean(string="Has Recording", default=False, index=True)

    note = fields.Text()

    # de-dup fingerprint
    fp_key = fields.Char(string="Fingerprint", index=True, readonly=True)

    _sql_constraints = [
        ("utel_unique", "unique(utel_id, company_id)", "Utel call already imported."),
        ("utel_fp_unique", "unique(company_id, fp_key)", "Duplicate call (fingerprint)."),
    ]

    # UI helpers
    player_html = fields.Html(string="Player", compute="_compute_player_html", sanitize=False)
    talk_time_display = fields.Char(string="Talk Time", compute="_compute_time_display", store=False)
    ring_time_display = fields.Char(string="Ring Time", compute="_compute_time_display", store=False)

    # ---------- computes ----------
    @api.depends("talk_time", "ring_time")
    def _compute_time_display(self):
        for rec in self:
            rec.talk_time_display = rec._format_seconds(rec.talk_time)
            rec.ring_time_display = rec._format_seconds(rec.ring_time)

    def _format_seconds(self, seconds: int) -> str:
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

    @api.depends("src", "dst", "external_number")
    def _compute_normals(self):
        for rec in self:
            rec.src_norm = _digits_only(rec.src)
            rec.dst_norm = _digits_only(rec.dst)
            rec.external_number_norm = _digits_only(rec.external_number)

    @api.depends("has_recording", "play_url")
    def _compute_player_html(self):
        for rec in self:
            rec.player_html = (
                f'<audio controls preload="none" style="width:220px;height:28px;">'
                f'<source src="/utel/stream/{rec.id}" type="audio/mpeg"/>'
                f"</audio>"
            ) if rec.has_recording else ""

    # ---------- timezone & date parsing ----------
    def _utel_tz(self) -> ZoneInfo:
        ICP = self.env["ir.config_parameter"].sudo()
        tzname = ICP.get_param(PARAM_TZ) or self.env.user.tz or "Asia/Tashkent"
        try:
            return ZoneInfo(tzname)
        except Exception:
            return ZoneInfo("Asia/Tashkent")

    def _parse_utel_datetime(self, txt):
        if not txt:
            return False
        s = str(txt).strip()
        try:
            if "T" in s or "Z" in s or "+" in s:
                s = s.replace("Z", "+00:00")
                dt = datetime.fromisoformat(s)
            else:
                dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except Exception:
            try:
                return fields.Datetime.to_datetime(s)
            except Exception:
                return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self._utel_tz())
        return dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    # ---------- fingerprint ----------
    def _build_fp_key_from_vals(self, vals):
        dt = vals.get("date_time")
        t = (vals.get("type") or "").strip()
        srcn = _digits_only(vals.get("src"))
        dstn = _digits_only(vals.get("dst"))
        extn = _digits_only(vals.get("external_number"))
        dt_s = dt.strftime("%Y-%m-%d %H:%M:%S") if isinstance(dt, datetime) and dt else str(dt or "")
        return f"{t}|{dt_s}|{srcn}|{dstn}|{extn}"

    def _recompute_fp_key(self):
        for r in self:
            dt_s = r.date_time.strftime("%Y-%m-%d %H:%M:%S") if r.date_time else ""
            r.fp_key = f"{r.type or ''}|{dt_s}|{r.src_norm or ''}|{r.dst_norm or ''}|{r.external_number_norm or ''}"

    # ---------- partner matching / auto-create ----------
    def _internal_extensions(self):
        ICP = self.env["ir.config_parameter"].sudo()
        raw = ICP.get_param(PARAM_INTERNAL_EXT, default="") or ""
        return {x.strip() for x in raw.split(",") if x.strip()}

    def _is_internal_number(self, raw_num) -> bool:
        if not raw_num:
            return True
        d = _digits_only(raw_num)
        if not d:
            return True
        if d in self._internal_extensions():
            return True
        if len(d) <= 5:
            return True
        return False

    def _company_dids(self):
        ICP = self.env["ir.config_parameter"].sudo()
        raw = (ICP.get_param(PARAM_DIDS) or "").strip()
        dids = set()
        for token in raw.split(","):
            d = _digits_only(token)
            if d:
                dids.add(d)
        return dids

    def _external_candidates(self):
        self.ensure_one()
        dids = self._company_dids()
        payload_did = _digits_only(self.external_number)
        if payload_did:
            dids.add(payload_did)

        out, seen = [], set()

        def consider(raw):
            d = _digits_only(raw)
            if not d or d in seen:
                return
            seen.add(d)
            if d in dids:
                return
            if self._is_internal_number(d):
                return
            out.append(_uz_digits(d))

        if (self.type or "").lower() == "in":
            for raw in (self.src, self.dst):
                consider(raw)
        elif (self.type or "").lower() == "out":
            for raw in (self.dst, self.src):
                consider(raw)
        else:
            for raw in (self.src, self.dst):
                consider(raw)

        if not out:
            for raw in (self.src, self.dst):
                d = _digits_only(raw)
                if d and not self._is_internal_number(d):
                    out.append(_uz_digits(d))
        return list(dict.fromkeys(out))

    def _find_partner_by_numbers(self):
        self.ensure_one()
        candidates = self._external_candidates()
        if not candidates:
            return self.env["res.partner"]

        Partners = self.env["res.partner"].sudo()

        # 1) try exact digits match by reading existing partners and comparing with _digits_only
        #    (we first narrow with a 7-digit tail ilike for performance)
        for d in candidates:
            tail = d[-7:] if len(d) > 7 else d
            possible = Partners.search(
                ['|', ('phone', 'ilike', tail), ('mobile', 'ilike', tail)],
                limit=50,
            )
            d_clean = _digits_only(d)
            for p in possible:
                if _digits_only(p.phone) == d_clean or _digits_only(p.mobile) == d_clean:
                    return p

        # 2) single narrow fallback — if exactly one record matches the tail, take it
        d0 = candidates[0]
        tail = d0[-7:] if len(d0) > 7 else d0
        narrowed = Partners.search(
            ['|', ('phone', 'ilike', tail), ('mobile', 'ilike', tail)],
            limit=2,
        )
        if len(narrowed) == 1:
            return narrowed[0]

        return self.env["res.partner"]

    def _assign_partner_from_numbers(self, create_if_missing=True):
        for rec in self:
            if rec.partner_id:
                continue
            partner = rec._find_partner_by_numbers()
            if partner:
                rec.partner_id = partner.id
                continue
            if not create_if_missing:
                continue
            cands = rec._external_candidates()
            if not cands:
                continue
            d = cands[0]
            pretty = _format_uz_pretty(_uz_digits(d))
            vals = {"name": pretty, "phone": pretty, "company_type": "person"}
            partner = self.env["res.partner"].sudo().create(vals)
            rec.partner_id = partner.id

    # ---------- upsert ----------
    def _upsert_from_vals(self, vals, company=None):
        company = company or self.env.company
        vals = dict(vals or {})
        vals.setdefault("company_id", company.id)
        vals["fp_key"] = self._build_fp_key_from_vals(vals)

        uid = (vals.get("utel_id") or "").strip()
        if uid:
            existing = self.sudo().with_company(company).search(
                [("utel_id", "=", uid), ("company_id", "=", company.id)], limit=1
            )
            if existing:
                existing.sudo().with_company(company).write(vals)
                return existing

        fp = vals["fp_key"]
        existing = self.sudo().with_company(company).search(
            [("fp_key", "=", fp), ("company_id", "=", company.id)], limit=1
        )
        if existing:
            existing.sudo().with_company(company).write(vals)
            return existing

        return self.sudo().with_company(company).create(vals)

    # ---------- ORM overrides ----------
    @api.model_create_multi
    def create(self, vals_list):
        for v in vals_list:
            v.setdefault("company_id", self.env.company.id)
            v["fp_key"] = self._build_fp_key_from_vals(v)
        records = super().create(vals_list)
        records._assign_partner_from_numbers(create_if_missing=True)
        return records

    def write(self, vals):
        res = super().write(vals)
        if {"src", "dst", "external_number", "date_time", "type"} & set(vals.keys()):
            self._recompute_fp_key()
            self._assign_partner_from_numbers(create_if_missing=True)
        return res

    # ---------- record actions ----------
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

    def action_delete_selected(self):
        self.check_access_rights("unlink")
        self.check_access_rule("unlink")
        self.unlink()
        return True

    # ---------- Notifications ----------
    def _notify_enabled(self) -> bool:
        ICP = self.env["ir.config_parameter"].sudo()
        val = (ICP.get_param(PARAM_NOTIFY_ENABLED) or "1").strip()
        return val not in {"0", "false", "False", ""}

    def _notify_target_users(self, company=None):
        company = company or self.env.company
        Users = self.env["res.users"].sudo()
        ICP = self.env["ir.config_parameter"].sudo()
        group_xmlid = (ICP.get_param(PARAM_NOTIFY_GROUP_XMLID) or "").strip()
        if group_xmlid:
            try:
                group = self.env.ref(group_xmlid)
            except Exception:
                group = None
            if group and group.users:
                return group.users.filtered(lambda u: u.active and (company in u.company_ids))
        return Users.search([
            ("active", "=", True),
            ("share", "=", False),
            ("company_ids", "in", company.id),
        ])

    def _bus_send_compat(self, notifications):
        Bus = self.env["bus.bus"].sudo()
        for meth in ("_sendmany", "sendmany"):
            fn = getattr(Bus, meth, None)
            if callable(fn):
                try:
                    fn(notifications)
                    return
                except TypeError:
                    pass
                except Exception:
                    pass
        for channel, message in notifications:
            for meth in ("_sendone", "sendone"):
                fn = getattr(Bus, meth, None)
                if callable(fn):
                    try:
                        fn(channel, message)
                        break
                    except TypeError:
                        continue
                    except Exception:
                        continue

    def _broadcast_new_calls(self, created_count: int, company=None):
        if not created_count or created_count <= 0:
            return
        if not self._notify_enabled():
            return
        company = company or self.env.company
        users = self._notify_target_users(company=company)
        if not users:
            return
        title = _("Utel")
        msg = _("%(n)s new call(s) imported.", n=created_count)

        dbname = self._cr.dbname
        payload = {"type": "simple_notification", "title": title, "message": msg, "sticky": False}
        notifications = [((dbname, "res.partner", u.partner_id.id), payload) for u in users if u.partner_id]
        if notifications:
            try:
                self._bus_send_compat(notifications)
            except Exception as e:
                _logger.debug("Utel broadcast failed: %s", e)

    # ---------- UTEL API sync ----------
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
            "per_page": per_page, "page": page, "sort": "-date_time",
            "filter[from]": date_from or "", "filter[to]": date_to or "",
            "filter[type]": "", "filter[status]": "",
            "filter[src]": "", "filter[dst]": "", "filter[external_number]": "",
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
        dt_raw = rec.get("date_time") or rec.get("datetime") or rec.get("time") or rec.get("started_at")
        dt = self._parse_utel_datetime(_as_text(dt_raw))

        talk_raw = rec.get("talk_time") or rec.get("conversation") or rec.get("duration")
        ring_raw = rec.get("ring_time") or rec.get("ringing") or rec.get("wait_time")
        talk_txt = _as_text(talk_raw)
        ring_txt = _as_text(ring_raw)
        talk_sec = _parse_hms(talk_txt) if ":" in talk_txt else int(talk_txt or 0)
        ring_sec = _parse_hms(ring_txt) if ":" in ring_txt else int(ring_txt or 0)

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

        play = (
            rec.get("recorded_file_url")
            or rec.get("play_url")
            or rec.get("listen_url")
            or rec.get("record_url")
            or rec.get("audio_url")
            or rec.get("stream_url")
        )
        download = rec.get("download_url") or rec.get("record_download_url") or rec.get("file_url") or ""

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
            "play_url": play or "",
            "download_url": download or "",
            "has_recording": bool(play or download),
        }

    @api.model
    def action_sync_all_pages(self, page_limit=5):
        company = self.env.company
        base, token, per_page = self._get_conn()
        ICP = self.env["ir.config_parameter"].sudo()
        days = int(ICP.get_param(PARAM_AUTO_DAYS, default="3") or 3)

        local_tz = self._utel_tz()
        now_local = datetime.now(local_tz)
        date_to_dt = now_local
        date_from_dt = now_local - timedelta(days=days)
        fmt = "%Y-%m-%d %H:%M:%S"
        date_from_str = date_from_dt.strftime(fmt)
        date_to_str = date_to_dt.strftime(fmt)

        processed = created = updated = 0

        for page in range(1, int(page_limit) + 1):
            payload = self._fetch_page(base, token, page=page, per_page=per_page,
                                       date_from=date_from_str, date_to=date_to_str)
            data = payload.get("data") or []
            if not data:
                break

            for rec in data:
                vals = self._to_vals(rec)
                if not vals.get("date_time"):
                    continue
                before = self.search_count([("fp_key", "=", self._build_fp_key_from_vals(vals)),
                                            ("company_id", "=", company.id)])
                obj = self._upsert_from_vals(vals, company=company)
                after = self.search_count([("fp_key", "=", obj.fp_key),
                                           ("company_id", "=", company.id)])
                if before and after:
                    updated += 1
                else:
                    created += 1
                processed += 1

            if len(data) < per_page:
                break

        try:
            self._broadcast_new_calls(created_count=created, company=company)
        except Exception as e:
            _logger.exception("Utel notify failed: %s", e)

        msg = _("Imported from Utel • processed: %(p)s • created: %(c)s • updated: %(u)s") % {
            "p": processed, "c": created, "u": updated
        }
        _logger.info(msg)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("Utel Sync"), "message": msg, "type": "success", "sticky": True},
        }

    @api.model
    def cron_sync_recent(self):
        try:
            self.action_sync_all_pages(page_limit=2)
            _logger.info("Utel cron executed.")
            return True
        except Exception as e:
            _logger.exception("Utel cron failed: %s", e)
            return False
