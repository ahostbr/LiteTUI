"""Fail-closed expert admission: reuse one resident model, never start a load.

The live server's status is evidence, not cached UI rows. This checks the target
server; it is not a GPU memory estimator or a reservation against unrelated apps.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

#: Fixed public services: no request to one of these can load a model on this machine.
REMOTE_BACKENDS = frozenset({'claude', 'codex', 'cline', 'free'})
#: Engines that load whatever model a request names. "Loaded now" is true for one
#: moment, so a seat on one of these asks again before every turn (seat_refusal).
LOADS_ON_REQUEST = frozenset({'lmstudio', 'strata'})


def resolve_host(host):
    """Every address the name resolves to. The one place this module asks DNS."""
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def _addresses(url):
    """Every address the URL's host has, or [] when that cannot be known."""
    host = (urlsplit(url).hostname or '').rstrip('.')
    if not host:
        return []
    if host == 'localhost' or host.endswith('.localhost'):
        host = '127.0.0.1'
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            # '127.1', '2130706433' and '0x7f.0.0.1' are loopback to the socket layer.
            addresses = [ipaddress.IPv4Address(socket.inet_aton(host))]
        except OSError:
            try:
                addresses = [ipaddress.ip_address(found.split('%')[0]) for found in resolve_host(host)]
            except (OSError, ValueError):
                return []
    return [address.ipv4_mapped if address.version == 6 and address.ipv4_mapped else address
            for address in addresses]


def url_is_local(url):
    """True unless EVERY address the URL's host has is a public one.

    Local: loopback, private, link-local, unspecified and every other range that
    is not globally routable; a name that does not resolve; a URL with no host.
    """
    addresses = _addresses(url)
    return not addresses or not all(address.is_global and not address.is_multicast for address in addresses)


def url_is_this_machine(url):
    """True only when EVERY address the URL's host has is a loopback one."""
    addresses = _addresses(url)
    return bool(addresses) and all(address.is_loopback for address in addresses)


def require_sole_resident(states, model):
    """The admission predicate: this model loaded, nothing loading, no other model loaded."""
    if not isinstance(states, dict) or any(value not in ('loaded', 'loading', 'unloaded') for value in states.values()):
        raise ValueError('Cannot verify local model loading status; child refused')
    if any(value == 'loading' for value in states.values()):
        raise ValueError('A local model is loading; wait until it finishes before running a child')
    if any(key != model and value == 'loaded' for key, value in states.items()):
        raise ValueError('A different local model is loaded; child refused to avoid a second VRAM model')
    if states.get(model) != 'loaded':
        raise ValueError('The requested local child model must be already loaded; subagents never load models')


async def admit_local(backend, model, enabled):
    if not enabled:
        raise ValueError('Local subagents require the expert allow_local_subagents toggle')
    status = getattr(backend, 'subagent_model_states', None)
    if not callable(status):
        raise ValueError('Cannot verify local model loading status; child refused')  # noqa: TRY004 - admission refusal contract
    require_sole_resident(await asyncio.to_thread(status), model)


def seat_refusal(backend, model):
    """Why a spawned worker must not send this request, or None. One read-only query.

    Only for an engine that would load `model` if a request named it while it is
    not loaded. A local custom server is such an engine only when it reports a
    state for every model; one that reports none is left exactly as before. A
    single-model llama.cpp server is one because it can sleep; a llama.cpp
    router is not asked here, its own readiness check refuses an unloaded model.
    """
    name = getattr(backend, 'name', None)
    custom = name == 'custom'
    if not (name in LOADS_ON_REQUEST or (custom and url_is_local(backend.base_url()))
            or (name == 'llamacpp' and getattr(backend, 'single_model', False))):
        return None
    try:
        states = backend.subagent_model_states()
    except Exception:  # noqa: BLE001 - unreadable state is not proof that the model is loaded
        states = None
    if not isinstance(states, dict):
        states = None
    if custom and (not states or 'unknown' in states.values()):
        return None
    if states is not None and states.get(model) == 'loaded':
        return None
    return (f'{model!r} is not loaded right now, or its server could not be read, so sending this '
            'request could load it. A spawned worker never loads a model: loading needs approval.')
