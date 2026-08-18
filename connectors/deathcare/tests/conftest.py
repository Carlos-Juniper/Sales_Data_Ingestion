import os
import sys

# connectors/ — for `from lib import ...`
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
# connectors/deathcare/ — for `import deathcare_merge as mod` etc.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
