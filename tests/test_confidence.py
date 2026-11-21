from renovate_butler import confidence as conf

from .conftest import badge_fetcher, body_for, fixture


def test_classifies_real_badges():
    assert conf.classify_badge(fixture("badge_high.svg")) == "high"
    assert conf.classify_badge(fixture("badge_low.svg")) == "low"
    assert conf.classify_badge(fixture("badge_neutral.svg")) == "neutral"
    assert conf.classify_badge(fixture("badge_very_high.svg")) == "very high"


def test_garbage_badge_is_unknown():
    assert conf.classify_badge("<svg>nothing</svg>") == conf.UNKNOWN
    assert conf.classify_badge('<svg><image xlink:href="data:image/png;base64,AAAA"/></svg>') == conf.UNKNOWN


def test_parses_real_renovate_body():
    c = conf.parse(fixture("body_npm_patch.md"), badge_fetcher({"i18next": "high"}))
    assert len(c.updates) == 1
    u = c.updates[0]
    assert (u.package, u.manager, u.from_version, u.to_version) == ("i18next", "npm", "27.1.1", "27.1.2")
    assert u.update_type == "patch"
    assert c.level == "high"


def test_body_without_badges_has_no_updates():
    c = conf.parse(fixture("body_no_confidence.md"), badge_fetcher({}))
    assert c.updates == []
    assert c.level == conf.UNKNOWN


def test_group_pr_takes_weakest_level():
    body = body_for("lodash", "4.17.20", "4.17.21") + "\n" + body_for("react", "17.0.2", "18.2.0").split("\n")[-1]
    c = conf.parse(body, badge_fetcher({"lodash": "high", "react": "low"}))
    assert c.level == "low"
    assert c.update_types == {"patch", "major"}


def test_fetch_error_means_unknown():
    def boom(url):
        raise OSError("offline")

    c = conf.parse(body_for("lodash", "1.0.0", "1.0.1"), boom)
    assert c.level == conf.UNKNOWN
    assert not conf.meets(c.level, "low")


def test_update_type():
    assert conf.update_type("1.2.3", "1.2.4") == "patch"
    assert conf.update_type("v1.2.3", "v1.3.0") == "minor"
    assert conf.update_type("1.2.3", "2.0.0") == "major"
    assert conf.update_type("0.3.1", "0.4.0") == "major"
    assert conf.update_type("abc", "def") == "unknown"


def test_meets():
    assert conf.meets("very high", "high")
    assert conf.meets("high", "high")
    assert not conf.meets("neutral", "high")
