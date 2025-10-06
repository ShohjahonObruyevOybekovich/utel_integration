# -*- coding: utf-8 -*-
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError

import os
import base64

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
_ICON_B64_CACHE = {}
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

def _read_static_b64(filename, module='utel_integration'):
    """
    Read file from addons/<module>/static/description/<filename> and return base64 string (ascii),
    or None on error. Uses in-process cache to avoid repeated disk reads.
    Replace 'your_module_name' with your actual module folder name.
    """
    key = f"{module}:{filename}"
    val = _ICON_B64_CACHE.get(key)
    if val is not None:
        return val
    # compute path relative to this file, assuming typical module layout:
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'static', 'description'))
    path = os.path.join(base_dir, filename)
    try:
        with open(path, 'rb') as fh:
            raw = fh.read()
        b64 = base64.b64encode(raw).decode('ascii')
        _ICON_B64_CACHE[key] = b64
        return b64
    except Exception:
        _ICON_B64_CACHE[key] = None
        return None

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
    
    type_icon_html = fields.Html(
        string="Type Icon HTML",
        compute="_compute_type_icon_html",
        readonly=True,
        sanitize=False,  
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


    @api.depends("type", "status", "src", "dst")
    def _compute_type_icon_html(self):
        """
        Show 4 icons based on direction (in/out) and answer status:
        - in + answered
        - in + not_answered
        - out + answered
        - out + not_answered

        Static files (put under static/description/):
        - call-in-answered.png
        - call-in-not-answered.png
        - call-out-answered.png
        - call-out-not-answered.png

        Fallbacks: small inline SVGs.
        """
        FILES = {
            "in_answered": "call-in-answered.png",
            "in_not_answered": "call-in-not-answered.png",
            "out_answered": "call-out-answered.png",
            "out_not_answered": "call-out-not-answered.png",
        }

        # tiny inline SVG fallbacks (16x16)
        SVG_FALLBACKS = {
            "in_answered": (
                '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
                'viewBox="0 0 24 24" style="vertical-align:middle">'
                '<path fill="currentColor" d="M21 15v4a1 1 0 0 1-1 1h-4a1 1 0 0 1-1-1v-1.2'
                'a1 1 0 0 0-.85-.99c-1.4-.24-2.75-.74-3.95-1.45a9 9 0 0 1-3.23-3.23'
                'c-.71-1.2-1.21-2.55-1.45-3.95A1 1 0 0 0 6.2 6H5a1 1 0 0 1-1-1V1'
                'a1 1 0 0 1 1-1h4c.55 0 1 .45 1 1v1.2c0 .33.2.63.51.79 3.12 1.58'
                '5.59 4.05 7.17 7.17.16.31.46.51.79.51H21z"/></svg>'
            ),
            "in_not_answered": (
                '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
                'viewBox="0 0 24 24" style="vertical-align:middle">'
                '<path fill="currentColor" d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm3.54 11.88'
                'L13.41 12l2.13-1.88a1 1 0 1 0-1.28-1.56L12 10.59 10.74 8.56'
                'a1 1 0 1 0-1.48 1.34L10.59 12l-1.33 1.88a1 1 0 0 0 1.48 1.34'
                'L12 13.41l1.26 1.81a1 1 0 0 0 1.48-1.34z"/></svg>'
            ),
            "out_answered": (
                '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
                'viewBox="0 0 24 24" style="vertical-align:middle">'
                '<path fill="currentColor" d="M3 5v4a1 1 0 0 0 1 1h1.2c.33 0 .63.2.79.51'
                '1.58 3.12 4.05 5.59 7.17 7.17.31.16.51.46.51.79V21a1 1 0 0 0 1 1h4'
                'a1 1 0 0 0 1-1v-4a1 1 0 0 0-1-1h-1.2c-.33 0-.63-.2-.79-.51'
                '-1.58-3.12-4.05-5.59-7.17-7.17-.31-.16-.51-.46-.51-.79V5a1 1 0 0 0-1-1H4'
                'a1 1 0 0 0-1 1z"/></svg>'
            ),
            "out_not_answered": (
                '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
                'viewBox="0 0 24 24" style="vertical-align:middle">'
                '<path fill="currentColor" d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm3.54 11.88'
                'L13.41 12l2.13-1.88a1 1 0 1 0-1.28-1.56L12 10.59 10.74 8.56'
                'a1 1 0 1 0-1.48 1.34L10.59 12l-1.33 1.88a1 1 0 0 0 1.48 1.34'
                'L12 13.41l1.26 1.81a1 1 0 0 0 1.48-1.34z"/></svg>'
            ),
        }

        MODULE_NAME = "utel_integration"  # your module folder

        def _answer_key(status_txt: str) -> str:
            s = (status_txt or "").strip().lower()
            if "not" in s or s == "no answer" or s == "missed":
                return "not_answered"
            if "answer" in s:
                return "answered"
            # default to not_answered if unclear
            return "not_answered"

        for rec in self:
            direction = (rec.type or "other").lower()

            # If upstream gave "missed" without direction, guess by numbers:
            if direction == "missed":
                # incoming missed if src looks external; else outgoing missed
                try:
                    if not rec._is_internal_number(rec.src):
                        direction = "in"
                    else:
                        direction = "out"
                except Exception:
                    direction = "in"

            if direction not in ("in", "out"):
                # fallback generic
                rec.type_icon_html = SVG_FALLBACKS["in_not_answered"]
                continue

            ans = _answer_key(rec.status)
            key = f"{direction}_{ans}"

            html = None
            filename = FILES.get(key)
            if filename:
                b64 = _read_static_b64(filename, module=MODULE_NAME)
                if b64:
                    ext = os.path.splitext(filename)[1].lower().lstrip(".")
                    mime = (
                        "image/png" if ext in ("png",)
                        else "image/svg+xml" if ext in ("svg",)
                        else "image/png"
                    )
                    html = (
                        f'<img src="data:{mime};base64,{b64}" '
                        f'style="width:16px;height:16px;object-fit:contain;vertical-align:middle;" '
                        f'alt="{key}"/>'
                    )

            if not html:
                html = SVG_FALLBACKS.get(key, SVG_FALLBACKS["in_not_answered"])

            rec.type_icon_html = html

        
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

        _NUM_TO_PARTNER_CACHE = {} 


    def _find_partner_by_numbers(self):
        """
        No custom fields; works with standard phone/mobile only.
        - Build canonical Uzbekistan digits (adds 998 for 9-digit).
        - First: quick hit from in-process cache.
        - Second: exact match on our COMPACT format (+<digits>).
        - Third: narrow with a 7-digit tail ilike, then verify by digits in Python.
        - Prefer same-company partner if multiple.
        """
        self.ensure_one()
        Partners = self.env["res.partner"].sudo()
        cache = globals().setdefault("_NUM_TO_PARTNER_CACHE", {})

        candidates = self._external_candidates()
        if not candidates:
            return self.env["res.partner"]

        # Canonicalize to clean Uzbekistan digits
        norms, seen = [], set()
        for raw in candidates:
            d = _digits_only(_uz_digits(raw))
            if d and d not in seen:
                seen.add(d)
                norms.append(d)

        if not norms:
            return self.env["res.partner"]

        for d_clean in norms:
            # 0) in-process cache (fast, avoids dupe during one sync)
            pid = cache.get(d_clean)
            if pid:
                p = Partners.browse(pid)
                if p.exists():
                    return p

            # 1) exact match on COMPACT format (what we create)
            compact = f"+{d_clean}"
            exact_eq = Partners.search(
                ['|', ('phone', '=', compact), ('mobile', '=', compact)],
                limit=1,
            )
            if exact_eq:
                cache[d_clean] = exact_eq.id
                return exact_eq

            # 2) fallback: tail ilike (works best when phones are stored compact)
            tail = d_clean[-7:] if len(d_clean) > 7 else d_clean
            possible = Partners.search(
                ['|', ('phone', 'ilike', tail), ('mobile', 'ilike', tail)],
                limit=100,
            )

            # verify strictly by digits to avoid false positives
            exact = []
            for p in possible:
                if _digits_only(p.phone) == d_clean or _digits_only(p.mobile) == d_clean:
                    exact.append(p)

            if exact:
                # Prefer same-company if possible
                same_co = [p for p in exact if (not p.company_id or p.company_id == self.env.company)]
                keeper = (same_co or exact)[0]
                cache[d_clean] = keeper.id
                return keeper

        return self.env["res.partner"]


    def _get_or_create_partner_by_number(self, raw_digits: str):
        """
        Idempotent without custom fields:
        - store phones in COMPACT format: '+<digits>' (no spaces),
        - lookup via cache, then exact '=', then tail ilike+verify,
        - if none found, create once; re-check and cache.
        """
        Partners = self.env["res.partner"].sudo()
        cache = globals().setdefault("_NUM_TO_PARTNER_CACHE", {})

        d_clean = _digits_only(_uz_digits(raw_digits))
        if not d_clean:
            return self.env["res.partner"]

        # 0) cache
        pid = cache.get(d_clean)
        if pid:
            p = Partners.browse(pid)
            if p.exists():
                return p

        compact = f"+{d_clean}"

        # 1) exact '=' on compact
        existing = Partners.search(
            ['|', ('phone', '=', compact), ('mobile', '=', compact)],
            limit=1,
        )
        if existing:
            cache[d_clean] = existing.id
            return existing

        # 2) tail ilike + verify
        tail = d_clean[-7:] if len(d_clean) > 7 else d_clean
        possible = Partners.search(
            ['|', ('phone', 'ilike', tail), ('mobile', 'ilike', tail)],
            limit=100,
        )
        for p in possible:
            if _digits_only(p.phone) == d_clean or _digits_only(p.mobile) == d_clean:
                cache[d_clean] = p.id
                return p

        # 3) create once in COMPACT format (no spaces → future tail matches)
        partner = Partners.create({
            "name": compact,
            "phone": compact,          # keep consistent & searchable
            "company_type": "person",
            # Optional company scoping:
            # "company_id": self.env.company.id,
        })

        # race-guard recheck
        again = Partners.search(
            ['|', ('phone', '=', compact), ('mobile', '=', compact)],
            limit=1,
        )
        final = again or partner
        cache[d_clean] = final.id
        return final
    
    def _assign_partner_from_numbers(self, create_if_missing=True):
        """
        Set partner_id for each record if possible.
        If create_if_missing=True (default), create a single canonical contact for the number.
        """
        for rec in self:
            if rec.partner_id:
                continue

            # First try to find an existing partner by strict norms
            partner = rec._find_partner_by_numbers()
            if partner:
                rec.partner_id = partner.id
                continue

            if not create_if_missing:
                continue

            # Create or get a single partner for the *first* external candidate
            cands = rec._external_candidates()
            if not cands:
                continue

            partner = rec._get_or_create_partner_by_number(cands[0])
            if partner:
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
