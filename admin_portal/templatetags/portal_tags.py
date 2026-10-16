import json
from django import template

register = template.Library()


@register.filter
def jsonify(value):
    try:
        return json.dumps(value, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)


@register.filter
def percentage(value):
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "0%"


@register.filter
def multiply(value, arg):
    try:
        return float(value) * float(arg)
    except (TypeError, ValueError):
        return 0


@register.filter
def get_item(mapping, key):
    """Look a key up in a dict from a template.

    Django's template language has no subscript syntax, so a lookup keyed on a
    loop variable — such as explaining an audit action from the vocabulary that
    defines it — needs a filter.
    """
    try:
        return (mapping or {}).get(key, "")
    except AttributeError:
        return ""
