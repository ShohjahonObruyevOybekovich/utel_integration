# utel_integration/controllers/sip.py
from urllib.parse import quote
from odoo import http
from odoo.http import request

_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<title>Calling {num}</title>
<meta http-equiv="refresh" content="0;url={scheme}:{urlnum}">
<style>body{{font:14px/1.5 system-ui;margin:24px}}a{{display:inline-block;margin-top:12px}}</style>
</head><body>
  <p>Launching your calling app to <strong>{num}</strong>…</p>
  <p><a href="{scheme}:{urlnum}">Open {scheme}:{num}</a></p>
  <p><a href="tel:{urlnum}">Or call via tel:{num}</a></p>
</body></html>"""

class SipController(http.Controller):

    # Preferred endpoint: /sip/call?n=+998...&scheme=sip
    @http.route("/sip/call", type="http", auth="user")
    def sip_call(self, n=None, scheme="sip", **kw):
        if not n:
            return request.not_found()
        raw = (n or "").replace(" ", "")
        return request.make_response(
            _HTML.format(num=raw, urlnum=quote(raw, safe="+"), scheme=scheme),
            headers=[("Content-Type", "text/html; charset=utf-8")]
        )

    # Backward-compat endpoint so /sip:+998... also works
    @http.route(["/sip/<path:number>", "/sip:+<path:number>"], type="http", auth="user")
    def sip_compat(self, number, **kw):
        raw = (number or "").replace(" ", "")
        return request.make_response(
            _HTML.format(num=raw, urlnum=quote(raw, safe="+"), scheme="sip"),
            headers=[("Content-Type", "text/html; charset=utf-8")]
        )
