"""Exercise geometry with synthetic artwork; no copyrighted character assets."""
import pytest

Image = pytest.importorskip("PIL.Image")

from scripts.tools.compose_portrait_layers import export_portraits


def fixture_layers(tmp_path):
    slices = tmp_path / "slices"
    slices.mkdir()
    body = Image.new("RGBA", (20, 30), "white")
    body.save(slices / "pose_1.png")
    eye = Image.new("RGBA", (2, 2), "red")
    eye.save(slices / "pose_2.png")
    mouth = Image.new("RGBA", (3, 1), "blue")
    mouth.save(slices / "pose_3.png")
    # Source origins intentionally differ, including a nonzero body origin.
    coords = {"pose": {"layers": [
        {"layer_id": 1, "z_order": 1, "hd": {"left": 40, "top": 60, "width": 20, "height": 30}},
        {"layer_id": 2, "z_order": 3, "hd": {"left": 44, "top": 66, "width": 2, "height": 2}},
        {"layer_id": 3, "z_order": 2, "hd": {"left": 47, "top": 73, "width": 3, "height": 1}},
    ]}}
    plan = {"costumes": [{"id": "uniform", "pose": "pose", "frame_layer": 1,
                           "sprites": {"eyes": [2, 1], "mouth": [3, 1]}}]}
    return slices, coords, plan


def test_each_differential_uses_its_own_origin_in_one_shared_frame(tmp_path):
    slices, coords, plan = fixture_layers(tmp_path)
    output = tmp_path / "export"
    export_portraits(coords, plan, slices, output, height=30)
    with Image.open(output / "uniform/eyes.png") as eyes, Image.open(output / "uniform/mouth.png") as mouth:
        assert eyes.size == mouth.size == (20, 30)
        assert eyes.getpixel((4, 6)) == (255, 0, 0, 255)
        assert mouth.getpixel((7, 13)) == (0, 0, 255, 255)
        assert mouth.getpixel((4, 6)) == (255, 255, 255, 255)


def test_wrong_resolution_rejects_entire_export_before_writing(tmp_path):
    slices, coords, plan = fixture_layers(tmp_path)
    Image.new("RGBA", (1, 1)).save(slices / "pose_3.png")
    output = tmp_path / "export"
    with pytest.raises(ValueError, match="HD coordinates"):
        export_portraits(coords, plan, slices, output)
    assert not output.exists()


def test_export_cannot_overwrite_installed_artwork(tmp_path):
    slices, coords, plan = fixture_layers(tmp_path)
    output = tmp_path / "installed"
    output.mkdir()
    original = output / "original.txt"
    original.write_text("preserved")
    with pytest.raises(ValueError, match="already exists"):
        export_portraits(coords, plan, slices, output)
    assert original.read_text() == "preserved"
