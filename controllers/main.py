# -*- coding: utf-8 -*-
import logging
import requests

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

class UtelController(http.Controller):

    @http.route('/utel/stream/<int:call_id>', type='http', auth='user')
    def utel_stream(self, call_id, **kw):
        """Proxy the remote recording and force inline playback."""
        call = request.env['utel.call'].sudo().browse(call_id)
        if not call.exists():
            return request.not_found()

        src_url = call.play_url or call.download_url
        if not src_url:
            return request.make_response("No recording URL.", [('Content-Type', 'text/plain')])

        try:
            # stream from UTEL
            r = requests.get(src_url, stream=True, timeout=60)
            r.raise_for_status()
        except Exception as e:
            _logger.exception("UTEL stream failed: %s", e)
            return request.make_response("Failed to fetch recording.", [('Content-Type', 'text/plain')])

        # pick content-type if present, fallback to audio/mpeg
        ctype = r.headers.get('Content-Type', 'audio/mpeg')
        # force inline so browsers don't download
        disp = f'inline; filename="utel_call_{call_id}.mp3"'

        # NOTE: we buffer in memory here; for very large files you could stream chunked
        data = r.content
        return request.make_response(
            data,
            headers=[
                ('Content-Type', ctype),
                ('Content-Disposition', disp),
                ('Cache-Control', 'no-cache'),
            ],
        )

    @http.route('/utel/player/<int:call_id>', type='http', auth='user')
    def utel_player(self, call_id, **kw):
        stream_url = f"/utel/stream/{call_id}"
        # ultra-compact player, ~420px wide, no margins
        html = f"""
                <!DOCTYPE html>
                <html>
                <head><meta charset="utf-8">
                <title>UTEL Recording #{call_id}</title>
                <style>
                  html,body {{ background:#111; margin:0; padding:10px; }}
                  .wrap {{ max-width: 440px; margin:0 auto; color:#eee; font: 14px/1.3 system-ui, -apple-system, Segoe UI, Roboto, Arial; }}
                  h4 {{ margin:0 0 6px; font-weight:600; }}
                  audio {{ width:100%; height:36px; }}
                </style>
                </head>
                <body>
                  <div class="wrap">
                    <h4>UTEL Recording #{call_id}</h4>
                    <audio controls autoplay>
                      <source src="{stream_url}">
                      Your browser does not support the audio element.
                    </audio>
                  </div>
                </body>
                </html>
                """
        return request.make_response(html, [('Content-Type', 'text/html; charset=utf-8')])

