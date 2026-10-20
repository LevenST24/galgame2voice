"""Acting menu construction and per-line act sanitization."""

import logging

import pytest

from galgame2voice.services.chat_pipelines.acting_menu import (
    FACE_LIMIT,
    build_acting_menu,
    sanitize_act,
)
from galgame2voice.services.chat_pipelines.line_scanner import FX, MOTIONS

LOGGER_NAME = "galgame2voice.services.chat_pipelines.acting_menu"


def portrait_payload() -> dict:
    return {
        "enabled": True,
        "default_costume": "uniform",
        "costumes": [
            {"id": "uniform", "name": "制服"},
            {"id": "casual", "name": "便服"},
        ],
        "sprites": {
            "uniform": {
                "smile": {"file": "smile.png"},
                "smile_alt": {"file": "smile_alt.png"},
                "blush_smile": {"file": "blush.png"},
                "angry": {"file": "angry.png"},
                "unlabeled": {"file": "unlabeled.png"},
            },
            "casual": {
                "smile": {"file": "smile.png"},
                "blush_smile": {"file": "blush.png"},
                "casual_face": {"file": "casual.png"},
            },
        },
        "faces": {
            "smile": {"label": "微笑", "blush": "blush_smile"},
            "smile_alt": {"label": "微笑", "blush": None},
            "blush_smile": {"label": "脸红微笑", "blush": None},
            "angry": {"label": "生气", "blush": None},
            "unlabeled": {"label": "", "blush": None},
            "casual_face": {"label": "便服脸", "blush": None},
        },
    }


MENU = {
    "outfit": "uniform",
    "faces": ["微笑", "生气"],
    "face_ids": {"微笑": ["smile", "smile_alt"], "生气": ["angry"]},
    "poses": ["a", "b"],
    "blush": True,
    "motions": list(MOTIONS),
    "fx": list(FX),
}


class TestBuildActingMenu:
    def test_labels_are_unique_ordered_and_skip_blush_targets(self):
        menu = build_acting_menu(portrait_payload())

        assert menu["outfit"] == "uniform"
        assert menu["faces"] == ["微笑", "生气"]
        assert menu["face_ids"] == {"微笑": ["smile", "smile_alt"], "生气": ["angry"]}
        assert menu["poses"] == []
        assert menu["blush"] is True
        assert menu["motions"] == list(MOTIONS)
        assert menu["fx"] == list(FX)

    def test_face_order_follows_first_face_id_not_alphabetical_label(self):
        payload = {
            "default_costume": "only",
            "costumes": [{"id": "only"}],
            "sprites": {"only": {"z_face": {}, "a_face": {}, "b_face": {}}},
            "faces": {
                "z_face": {"label": "Zeta", "blush": None},
                "a_face": {"label": "Alpha", "blush": None},
                "b_face": {"label": "Beta", "blush": None},
            },
        }
        assert build_acting_menu(payload)["faces"] == ["Zeta", "Alpha", "Beta"]

    def test_requested_outfit_wins_then_default_then_first(self):
        payload = portrait_payload()
        assert build_acting_menu(payload, "casual")["outfit"] == "casual"
        assert build_acting_menu(payload, "missing")["outfit"] == "uniform"

        del payload["default_costume"]
        assert build_acting_menu(payload, "missing")["outfit"] == "uniform"

        payload["default_costume"] = "does-not-exist"
        assert build_acting_menu(payload)["outfit"] == "uniform"

    def test_casual_outfit_reports_its_own_faces(self):
        menu = build_acting_menu(portrait_payload(), "casual")
        assert menu["faces"] == ["微笑", "便服脸"]
        assert menu["face_ids"] == {"微笑": ["smile"], "便服脸": ["casual_face"]}
        assert menu["blush"] is True

    def test_blush_is_false_when_no_listed_face_has_a_variant(self):
        payload = portrait_payload()
        del payload["sprites"]["uniform"]["blush_smile"]
        assert build_acting_menu(payload)["blush"] is False

    def test_blush_variant_must_exist_in_the_resolved_outfit(self):
        payload = portrait_payload()
        payload["sprites"]["casual"].pop("blush_smile")
        assert build_acting_menu(payload, "casual")["blush"] is False

    def test_menu_is_capped_at_forty_labels(self):
        payload = {
            "default_costume": "only",
            "costumes": [{"id": "only"}],
            "sprites": {
                "only": {f"face-{i}": {"file": f"{i}.png"} for i in range(FACE_LIMIT + 5)}
            },
            "faces": {
                f"face-{i}": {"label": f"标签{i}", "blush": None}
                for i in range(FACE_LIMIT + 5)
            },
        }
        menu = build_acting_menu(payload)

        assert len(menu["faces"]) == FACE_LIMIT
        assert menu["faces"][0] == "标签0"
        assert menu["faces"][-1] == f"标签{FACE_LIMIT - 1}"
        assert f"标签{FACE_LIMIT}" not in menu["face_ids"]

    def test_repeated_label_after_the_cap_does_not_grow_the_list(self):
        payload = {
            "default_costume": "only",
            "costumes": [{"id": "only"}],
            "sprites": {
                "only": {f"face-{i}": {} for i in range(FACE_LIMIT)}
                | {"overflow": {}},
            },
            "faces": {f"face-{i}": {"label": f"标签{i}", "blush": None} for i in range(FACE_LIMIT)}
            | {"overflow": {"label": "标签0", "blush": None}},
        }
        menu = build_acting_menu(payload)

        assert len(menu["faces"]) == FACE_LIMIT
        assert menu["face_ids"]["标签0"] == ["face-0", "overflow"]

    def test_no_portrait_returns_none(self):
        assert build_acting_menu(None) is None
        assert build_acting_menu(None, "uniform") is None


class TestSanitizeAct:
    def test_all_valid_fields_survive(self):
        act = {"face": "微笑", "pose": "a", "motion": "nod", "blush": True, "fx": "heart"}
        assert sanitize_act(act, MENU, line_index=0, character="测试") == act

    def test_each_invalid_field_is_dropped_and_logged_while_valid_ones_survive(self, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        act = {
            "face": "不存在",
            "pose": "c",
            "motion": "laugh",
            "blush": "yes",
            "fx": "flash",
            "outfit": "casual",
            "extra": 1,
        }

        clean = sanitize_act(act, MENU, line_index=7, character="测试角色")

        assert clean == {}
        text = caplog.text
        for field, value in act.items():
            assert field in text
            assert repr(value)[:40] in text
        assert "测试角色" in text
        assert "line 7" in text
        assert text.count("unsupported act key") == 2

    def test_valid_fields_survive_alongside_dropped_ones(self, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        act = {"face": "微笑", "motion": "laugh", "blush": True, "fx": "heart"}

        assert sanitize_act(act, MENU, line_index=3, character="测试") == {
            "face": "微笑",
            "blush": True,
            "fx": "heart",
        }
        assert "motion not allowed" in caplog.text

    def test_motion_and_fx_accept_none(self):
        act = {"motion": None, "fx": None}
        assert sanitize_act(act, MENU, line_index=0, character="测试") == act

    def test_wrong_types_are_dropped(self, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        act = {"face": 1, "blush": 1}

        assert sanitize_act(act, MENU, line_index=0, character="测试") == {}
        assert "face must be a string" in caplog.text
        assert "blush must be a boolean" in caplog.text

    def test_blush_true_requires_menu_capability(self, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        menu = {**MENU, "blush": False}

        assert sanitize_act({"blush": True}, menu, line_index=0, character="测试") == {}
        assert "blush not available" in caplog.text

    def test_blush_false_is_always_valid(self):
        menu = {**MENU, "blush": False}
        assert sanitize_act({"blush": False}, menu, line_index=0, character="测试") == {
            "blush": False
        }

    def test_no_menu_drops_face_and_pose_but_keeps_other_fields(self, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        act = {"face": "微笑", "pose": "a", "motion": "nod", "blush": False, "fx": "heart"}

        assert sanitize_act(act, None, line_index=2, character="测试") == {
            "motion": "nod",
            "blush": False,
            "fx": "heart",
        }
        assert caplog.text.count("no acting menu") == 2
        assert "face" in caplog.text
        assert "pose" in caplog.text

    def test_no_menu_rejects_true_blush(self, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        assert sanitize_act({"blush": True}, None, line_index=0, character="测试") == {}
        assert "blush not available" in caplog.text

    def test_non_dict_becomes_empty_and_is_logged(self, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        assert sanitize_act(["face"], MENU, line_index=4, character="测试") == {}
        assert "act is not a dict" in caplog.text
        assert "line 4" in caplog.text

    def test_logged_value_is_cut_to_forty_characters(self, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        value = "x" * 100

        sanitize_act({"face": value}, MENU, line_index=1, character="测试")

        assert repr(value)[:40] in caplog.text
        assert repr(value) not in caplog.text

    @pytest.mark.parametrize("value", ["nod", "bounce", "startle", "sigh", "fidget", "head-shake", "lean-in", "step-back"])
    def test_every_motion_is_accepted(self, value):
        assert sanitize_act({"motion": value}, MENU, line_index=0, character="测试") == {
            "motion": value
        }

    @pytest.mark.parametrize("value", ["sweat", "anger", "heart", "sparkle", "question", "exclamation"])
    def test_every_fx_is_accepted(self, value):
        assert sanitize_act({"fx": value}, MENU, line_index=0, character="测试") == {
            "fx": value
        }
