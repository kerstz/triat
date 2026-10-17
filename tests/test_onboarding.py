"""Tests de l'écran de bienvenue."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import pytest
from gi.repository import Adw

from triat.core.engine import Engine
from triat.core.library import Library
from triat.core.settings import Settings
from triat.ui.onboarding import STEPS, OnboardingView
from tests.fakes import (FakeCookies, FakeExtractor, FakeInnerTube,
                         FakePlayer, FakeRecommender, FakeSponsorBlock)

Adw.init()


@pytest.fixture
def engine(tmp_path):
    settings = Settings(tmp_path / "s.json")
    eng = Engine(settings=settings, library=Library(tmp_path / "l.db"),
                 player=FakePlayer(), extractor=FakeExtractor(),
                 sponsorblock=FakeSponsorBlock(), recommender=FakeRecommender(),
                 ytmusic=object(), cookies=FakeCookies(),
                 innertube=FakeInnerTube())
    yield eng
    eng.library.close()


def test_starts_on_the_first_step(engine):
    view = OnboardingView(engine, lambda: None)
    assert view._step == 0
    assert view._step_label.get_text().startswith("Étape 1 sur")


def test_walks_through_every_step(engine):
    done = []
    view = OnboardingView(engine, lambda: done.append(True))
    for _ in range(STEPS - 1):
        view._next()
    assert view._step == STEPS - 1
    assert not done, "pas fini avant la dernière étape"
    view._next()
    assert done == [True]


def test_finishing_marks_the_setting(engine):
    view = OnboardingView(engine, lambda: None)
    for _ in range(STEPS):
        view._next()
    assert engine.settings.get("onboarding_done") is True


def test_sponsorblock_toggle_persists(engine):
    view = OnboardingView(engine, lambda: None)
    view._set("sponsorblock_enabled", False)
    assert engine.settings.get("sponsorblock_enabled") is False


def test_browser_detection_returns_known_names(engine):
    detected = OnboardingView._detect_browsers()
    assert isinstance(detected, list)
    assert all(isinstance(name, str) for name in detected)


def test_skip_is_only_offered_on_the_account_step(engine):
    view = OnboardingView(engine, lambda: None)
    assert view._skip.get_visible()
    view._next()
    assert not view._skip.get_visible()


def test_soundcloud_off_by_default_in_a_collapsed_row(engine):
    view = OnboardingView(engine, lambda: None)
    assert not view._soundcloud.row.get_active()
    assert not view._more_sources.get_expanded()
    assert not engine.settings.get("soundcloud_enabled")


def test_soundcloud_cancel_writes_nothing(engine):
    view = OnboardingView(engine, lambda: None)
    view._soundcloud.row.set_active(True)
    assert view._soundcloud.alert is not None
    view._soundcloud.alert.emit("response", "cancel")
    assert not view._soundcloud.row.get_active()
    assert not engine.settings.get("soundcloud_enabled")
    assert not engine.settings.get("soundcloud_tos_ack")


def test_soundcloud_enable_then_no_second_box(engine):
    view = OnboardingView(engine, lambda: None)
    view._soundcloud.row.set_active(True)
    view._soundcloud.alert.emit("response", "enable")
    assert engine.settings.get("soundcloud_tos_ack") is True
    assert engine.settings.get("soundcloud_enabled") is True
    view._soundcloud.row.set_active(False)
    view._soundcloud.row.set_active(True)
    assert view._soundcloud.alert is None
