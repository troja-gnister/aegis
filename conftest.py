import os

from aegisctl.container_engine import local_environment

from tests.support.database_roles import role_database as role_database

# Deployment subprocesses may inherit application inputs, but never an operator's
# remote container routing. Compose adds its validated temporary socket per call.
_local_environment = local_environment(os.environ)
os.environ.clear()
os.environ.update(_local_environment)
