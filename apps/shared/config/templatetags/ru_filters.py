from django import template

register = template.Library()


@register.filter(name='ru_plural')
def ru_plural(value, forms):
    """Russian pluralization.

    Usage: ``{{ n|ru_plural:"клиент,клиента,клиентов" }}``.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return ''
    parts = [p.strip() for p in forms.split(',')]
    if len(parts) != 3:
        return ''
    one, few, many = parts
    mod10 = abs(n) % 10
    mod100 = abs(n) % 100
    if mod10 == 1 and mod100 != 11:
        return one
    if 2 <= mod10 <= 4 and not (12 <= mod100 <= 14):
        return few
    return many
