import json
from django import template

register = template.Library()


@register.filter
def jsonify(value):
    """Pretty-print a dict/list as JSON."""
    try:
        return json.dumps(value, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)


@register.filter
def percentage(value):
    """Convert 0-1 float to percentage string."""
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "0%"


@register.filter
def severity_icon(severity):
    icons = {
        "info": "ℹ️",
        "warning": "⚠️",
        "critical": "🚨",
    }
    return icons.get(severity, "")
