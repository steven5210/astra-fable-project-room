'''Importable test-only fault hook.

This module is deliberately unreachable from CLI arguments, environment
variables, the envelope or drafts.  Tests import it and install a hook.
'''
_HOOK = None

def set_hook(hook):
    global _HOOK
    _HOOK = hook

def clear_hook():
    global _HOOK
    _HOOK = None

def fault(point, **detail):
    hook = _HOOK
    if hook is not None:
        return hook(point, detail)
    return None
