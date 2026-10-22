"""Per-connection scoping, a regression guard for the SDK 2.x port.

SDK 1.x scoped processes by ``Context.client_id``, which only ever echoed a
non-standard ``client_id`` key from a request's ``_meta``. Nothing populates
that, so every real client shared one ``ProcessManager`` and the unit tests
could not tell, because their doubles injected a random id. SDK 2.x removed
the attribute, so ``_client_key`` resolves the connection from the transport.
"""

import types

import async_bash_mcp as abm


class FakeRequest:
    def __init__(self, headers=None, query_params=None):
        self.headers = headers or {}
        self.query_params = query_params or {}


def make_ctx(*, headers=None, query=None, client_id=None, with_request=True):
    ctx = types.SimpleNamespace()
    request = None
    if with_request and (headers or query):
        request = FakeRequest(headers, query)
    ctx.request_context = types.SimpleNamespace(meta=None, request=request)
    if client_id is not None:
        ctx.client_id = client_id
    return ctx


def test_http_session_id_is_stable_across_requests():
    """Each request gets a fresh Context, so the key cannot come from it."""
    first = make_ctx(headers={"mcp-session-id": "abc"})
    second = make_ctx(headers={"mcp-session-id": "abc"})

    assert abm._client_key(first) == abm._client_key(second) == "http:abc"


def test_http_sessions_are_isolated():
    first = make_ctx(headers={"mcp-session-id": "abc"})
    second = make_ctx(headers={"mcp-session-id": "def"})

    assert abm._client_key(first) != abm._client_key(second)


def test_sse_session_comes_from_the_query_string():
    ctx = make_ctx(query={"session_id": "s1"})

    assert abm._client_key(ctx) == "sse:s1"


def test_stdio_is_a_single_client():
    assert abm._client_key(make_ctx()) == "stdio"


def test_injected_client_id_still_works():
    ctx = make_ctx(client_id="legacy")

    assert abm._client_key(ctx) == "legacy"


def test_empty_manager_is_pruned():
    pm = abm.get_client_process_manager("prune-me")
    assert abm.client_managers["prune-me"] is pm

    abm._drop_client_manager_if_empty("prune-me", pm)

    assert "prune-me" not in abm.client_managers


def test_manager_with_processes_is_kept():
    pm = abm.get_client_process_manager("busy")
    pm._processes[1] = object()
    try:
        abm._drop_client_manager_if_empty("busy", pm)

        assert abm.client_managers["busy"] is pm
    finally:
        pm._processes.clear()
        abm.client_managers.pop("busy", None)


def test_manager_with_spawn_in_flight_is_kept():
    """The reaper must not drop a manager between spawn and registration."""
    pm = abm.get_client_process_manager("spawning")
    pm._pending = 1
    try:
        assert not pm.is_empty()

        abm._drop_client_manager_if_empty("spawning", pm)

        assert abm.client_managers["spawning"] is pm
    finally:
        pm._pending = 0
        abm.client_managers.pop("spawning", None)
