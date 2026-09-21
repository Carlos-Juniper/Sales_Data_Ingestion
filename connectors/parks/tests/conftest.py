import os
import sys

# connectors/ — for `from lib import ...`
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
# connectors/parks/ — for `import park_layers as mod` etc.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
