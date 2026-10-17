import pytest

from tests.test_me_cards import AUTH, as_user  # noqa: F401

DEFAULTS = {"favorites": [], "recentReports": [], "readNotifications": [],
            "visibleWidgets": ["ai", "kpis", "feed", "quickaccess"], "version": 0}
BODY = {"favorites": ["r1"], "recentReports": ["r2"], "readNotifications": ["n1"], "visibleWidgets": ["kpis"],
        "expected_version": 0}


def test_defaults(as_user):
    r = as_user().get("/api/v1/me/prefs", headers=AUTH)
    assert r.status_code == 200 and r.json() == DEFAULTS


def test_put_then_get_and_version_increments(as_user):
    c = as_user()
    r = c.put("/api/v1/me/prefs", headers=AUTH, json=BODY)
    assert r.status_code == 200 and r.json()["version"] == 1 and r.json()["favorites"] == ["r1"]
    r2 = c.put("/api/v1/me/prefs", headers=AUTH, json={**BODY, "favorites": [], "expected_version": 1})
    assert r2.json()["version"] == 2
    assert c.get("/api/v1/me/prefs", headers=AUTH).json()["favorites"] == []


def test_stale_version_is_409(as_user):
    c = as_user()
    c.put("/api/v1/me/prefs", headers=AUTH, json=BODY)
    assert c.put("/api/v1/me/prefs", headers=AUTH, json=BODY).status_code == 409
    assert c.put("/api/v1/me/prefs", headers=AUTH, json={**BODY, "expected_version": 7}).status_code == 409


@pytest.mark.parametrize("patch,fragment", [
    ({"favorites": [str(i) for i in range(201)]}, "favorites can hold at most 200"),
    ({"readNotifications": [str(i) for i in range(201)]}, "readNotifications can hold at most 200"),
    ({"recentReports": list("abcdef")}, "recentReports can hold at most 5"),
    ({"visibleWidgets": ["ai", "bogus"]}, "unknown widgets: bogus"),
    ({"favorites": "x"}, "favorites must be a list"),
    ({"favorites": [1]}, "favorites must be a list"),
    ({"expected_version": -1}, "expected_version"),
    ({"expected_version": "0"}, "expected_version"),
])
def test_invalid_is_400(as_user, patch, fragment):
    r = as_user().put("/api/v1/me/prefs", headers=AUTH, json={**BODY, **patch})
    assert r.status_code == 400
    assert any(fragment in e for e in r.json()["detail"]["errors"])
