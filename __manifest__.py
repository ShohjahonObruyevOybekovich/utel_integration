{
    "name": "Utel IP Telephony Integration",
    "summary": "Sync & view Utel call history inside Odoo",
    "version": "18.0.1.0.2",
    "category": "Productivity/VoIP",
    "author": "Your Company",
    "license": "LGPL-3",
    "depends": ["base"],
    "data": [
        'security/ir.model.access.csv',
        "views/utel_call_views.xml",
        "views/res_config_settings_views.xml",  # standalone settings form
        "data/ir_cron.xml",
    ],
    "application": True,
    "icon": "utel_integration/static/description/icon.png",
}
