import unittest

from active_view_preview import consensus
from vision_preview import Preview, orient_image
from PIL import Image


def sample(direction):
    return Preview(direction, "test", {"LEFT": 80, "CENTER": 20, "RIGHT": 10})


class ActiveViewTests(unittest.TestCase):
    def test_requires_three_pictures(self):
        self.assertEqual(consensus([sample("LEFT"), sample("LEFT")])[0], "STOP")

    def test_disagreement_stops(self):
        self.assertEqual(consensus([sample("LEFT"), sample("RIGHT"), sample("LEFT")])[0], "STOP")

    def test_uncertain_picture_stops(self):
        self.assertEqual(consensus([sample("LEFT"), sample("STOP"), sample("LEFT")])[0], "STOP")

    def test_agreement_is_tentative(self):
        self.assertEqual(consensus([sample("RIGHT")] * 3)[0], "RIGHT")

    def test_rotation_changes_image_orientation(self):
        image = Image.new("RGB", (2, 2), "black")
        image.putpixel((0, 0), (255, 0, 0))
        self.assertEqual(orient_image(image, 180).getpixel((1, 1)), (255, 0, 0))


if __name__ == "__main__":
    unittest.main()
