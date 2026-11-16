""": 지도 탭의 '자주 가는 장소' 카드 제거. /api/places와 places 표는 주행·충전 장소 이름에 쓰여 유지."""
import unittest
from pathlib import Path

HTML = (Path(__file__).resolve().parents[1] / "web" / "static" / "index.html").read_text(encoding="utf-8")


class NoPlacesCardTests(unittest.TestCase):
    def test_card_and_handlers_gone(self) -> None:
        for s in ("자주 가는 장소", 'id="places-list"', 'id="place-save"', "class=\"place-form\"", ".place-form", "refreshPlaces",
                  "placePick", "mapPickGroup", "places-help"):
            self.assertNotIn(s, HTML, s)

    def test_map_still_there(self) -> None:
        # : the map tab is gone; trips and charges carry their own maps
        for s in ('id="trips-map"', 'id="charges-map"', "function loadLeaflet"):
            self.assertIn(s, HTML, s)
        self.assertNotIn('id="map-view"', HTML)


if __name__ == "__main__":
    unittest.main()
