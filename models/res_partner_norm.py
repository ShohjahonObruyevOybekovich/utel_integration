# -*- coding: utf-8 -*-
from odoo import api, fields, models

def _digits_only(s):
    return ''.join(ch for ch in str(s or '') if ch.isdigit())

class ResPartner(models.Model):
    _inherit = "res.partner"

    # Stored, indexed helper fields – created by this module on upgrade
    phone_norm = fields.Char(index=True, compute="_compute_phone_norms", store=True)
    mobile_norm = fields.Char(index=True, compute="_compute_phone_norms", store=True)

    @api.depends('phone', 'mobile')
    def _compute_phone_norms(self):
        for rec in self:
            rec.phone_norm = _digits_only(rec.phone)
            rec.mobile_norm = _digits_only(rec.mobile)
