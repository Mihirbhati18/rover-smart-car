import unittest

from PIL import Image, ImageDraw

from vision_preview import inspect_image


class VisionPreviewTests(unittest.TestCase):
    def test_dark_frame_stops(self):
        image = Image.new("RGB", (300, 240), "black")
        self.assertEqual(inspect_image(image).suggestion, "STOP")

    def test_indistinguishable_clear_zones_stop(self):
        image = Image.new("RGB", (300, 240), (110, 110, 110))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 299, 20), fill=(140, 140, 140))
        self.assertEqual(inspect_image(image).suggestion, "STOP")

    def test_clear_left_is_tentatively_selected(self):
        image = Image.new("RGB", (300, 240), (110, 110, 110))
        draw = ImageDraw.Draw(image)
        draw.rectangle((105, 110, 299, 190), fill=(230, 35, 35))
        result = inspect_image(image)
        self.assertEqual(result.suggestion, "LEFT")
        self.assertGreater(result.clear_percent["LEFT"], result.clear_percent["CENTER"])

    def test_clear_right_is_tentatively_selected(self):
        image = Image.new("RGB", (300, 240), (110, 110, 110))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 110, 195, 190), fill=(35, 35, 230))
        self.assertEqual(inspect_image(image).suggestion, "RIGHT")


if __name__ == "__main__":
    unittest.main()
