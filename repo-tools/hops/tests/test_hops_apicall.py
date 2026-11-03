"""The shared `call`/`batch` front end (hops.apicall), against a fake backend."""

import pytest

from hops import apicall
from hops.errors import CallError, HopsError, UsageError


class FakeBackend:
    """A backend whose `answers` map a method to its raw result, or to a CallError it raises. It refuses methods
    starting `write.` and masks fields named `secret`."""

    api = "fake"
    capability = "truenas"

    def __init__(self, answers=None, *, open_error=None):
        self.answers = answers or {}
        self.open_error = open_error
        self.opens = 0
        self.sent: list[tuple[str, list]] = []

    def check(self, method, params):
        if method.startswith("write."):
            raise UsageError(f"{method} is not a read method")

    def open(self):
        self.opens += 1
        if self.open_error:
            raise self.open_error

    def call(self, method, params):
        assert self.opens == 1, "called before open"
        self.sent.append((method, params))
        answer = self.answers[method]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def redact(self, result, method):
        return {k: "[REDACTED]" if k == "secret" else v for k, v in result.items()}


def test_call_returns_the_redacted_result():
    backend = FakeBackend({"a.read": {"name": "x", "secret": "fake-secret"}})
    assert apicall.call(backend, "a.read", [1]) == {"name": "x", "secret": "[REDACTED]"}
    assert backend.sent == [("a.read", [1])]


def test_call_refuses_before_connecting():
    backend = FakeBackend()
    with pytest.raises(UsageError, match="not a read method"):
        apicall.call(backend, "write.it", [])
    assert backend.opens == 0


def test_batch_opens_once_and_redacts_each_result():
    backend = FakeBackend({"a.read": {"secret": "fake-a"}, "b.read": {"name": "b", "secret": "fake-b"}})
    results = apicall.batch(backend, [("a.read", []), ("b.read", [["id", "=", 1]]), ("a.read", [])])
    assert results == [
        {"method": "a.read", "params": [], "result": {"secret": "[REDACTED]"}},
        {"method": "b.read", "params": [["id", "=", 1]], "result": {"name": "b", "secret": "[REDACTED]"}},
        {"method": "a.read", "params": [], "result": {"secret": "[REDACTED]"}},
    ]
    assert backend.opens == 1


def test_a_refused_call_inside_a_valid_batch_is_reported_and_never_sent():
    backend = FakeBackend({"a.read": {"name": "a"}})
    results = apicall.batch(backend, [("a.read", []), ("write.it", [{"x": 1}]), ("a.read", [])])
    assert results[1] == {"method": "write.it", "params": [{"x": 1}], "error": "write.it is not a read method"}
    assert [r.get("result") for r in results] == [{"name": "a"}, None, {"name": "a"}]
    assert backend.sent == [("a.read", []), ("a.read", [])]


def test_a_per_call_error_does_not_abort_the_batch():
    backend = FakeBackend({"a.read": CallError("fake a.read: ENOENT: no such thing"), "b.read": {"name": "b"}})
    results = apicall.batch(backend, [("a.read", []), ("b.read", [])])
    assert results == [
        {"method": "a.read", "params": [], "error": "fake a.read: ENOENT: no such thing"},
        {"method": "b.read", "params": [], "result": {"name": "b"}},
    ]


def test_a_batch_of_only_refused_calls_never_connects():
    backend = FakeBackend()
    assert apicall.batch(backend, [("write.it", [])])[0]["error"] == "write.it is not a read method"
    assert backend.opens == 0


def test_a_failed_login_aborts_the_batch():
    backend = FakeBackend({"a.read": {}}, open_error=HopsError("fake rejected the key"))
    with pytest.raises(HopsError, match="rejected the key"):
        apicall.batch(backend, [("a.read", [])])


def test_a_lost_connection_aborts_the_batch_instead_of_logging_in_again():
    backend = FakeBackend({"a.read": HopsError("cannot reach fake"), "b.read": {}})
    with pytest.raises(HopsError, match="cannot reach fake"):
        apicall.batch(backend, [("a.read", []), ("b.read", [])])
    assert backend.opens == 1 and backend.sent == [("a.read", [])]


@pytest.mark.parametrize(
    "text, problem",
    [
        ("not json", "not JSON"),
        ('{"method": "a.read"}', "a JSON list"),
        ('["a.read"]', "call 1: must be an object"),
        ('[{"params": []}]', "call 1: method must be a non-empty string"),
        ('[{"method": "a.read", "params": {"x": 1}}]', "call 1: params must be a list"),
        ('[{"method": "a.read"}, {"method": "b.read", "id": 3}]', "call 2: unknown key id"),
    ],
)
def test_malformed_batch_input_is_a_usage_error(text, problem):
    with pytest.raises(UsageError, match=problem):
        apicall.parse_calls(text)


def test_params_default_to_an_empty_list():
    assert apicall.parse_calls('[{"method": "a.read"}, {"method": "b.read", "params": [1]}]') == [
        ("a.read", []),
        ("b.read", [1]),
    ]
