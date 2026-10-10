"""A sample domain profile with one page of its own, for the Studio's project slot (docs/studio.md
4.0). studio_fixture.add_sample_profile copies this package into a fixture project; nothing here edits
alpaca/web. The page reads its own route, /hub/bench.json, never the overview."""
import os

from alpaca.profile import Profile

HERE = os.path.dirname(os.path.abspath(__file__))
READINGS = [{"name": "Bench A", "value": "21.5 C"}, {"name": "Bench B", "value": "19.0 C"}]


class Bench(Profile):
    name = "bench"

    def routes(self):
        return {"/hub/bench.json": lambda root, query: ({"items": READINGS}, 200)}

    def web(self):
        return {"pages": [{"id": "bench", "label": "Bench", "module": "bench.js", "css": "bench.css",
                           "icon": "research", "nav": 5.5}],
                "css": ["look.css"],
                "assets": {name: os.path.join(HERE, name) for name in ("bench.js", "bench.css", "look.css")}}


PROFILE = Bench
