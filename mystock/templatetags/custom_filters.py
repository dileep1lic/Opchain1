from django import template

register = template.Library()

@register.filter
def get_item(dictionary, key):
    """Dictionary से key से value निकालने के लिए"""
    return dictionary.get(key)
