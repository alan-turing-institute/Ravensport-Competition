"""The episode runner of the RavensPort competition (episodes.md).

The episode runner shares the render pipeline's code, `deploy/scripts/render.py`,
so that a development deployment and an episode derive their seeds, start-up
input, monitored hosts and scan interval alike. The package puts `deploy/scripts`
on the module search path, because `render.py` imports `world_schema` from its
own directory.
"""

import sys
from pathlib import Path

_DEPLOY_SCRIPTS = Path(__file__).resolve().parents[2] / "deploy" / "scripts"
if str(_DEPLOY_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_DEPLOY_SCRIPTS))
