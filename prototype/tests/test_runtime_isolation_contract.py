from __future__ import annotations

import os
import unittest
from unittest.mock import patch

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from app.infrastructure.runtime_settings import (
    AgentRuntimeBackend,
    DatabaseBackend,
    DeploymentMode,
    ObjectStoreBackend,
    RuntimeSettings,
)


class RuntimeIsolationContractTests(unittest.TestCase):
    def test_reactivation_overrides_inherited_local_and_cloud_paths(self) -> None:
        storage_dir = activate_test_runtime()

        with patch.dict(
            os.environ,
            {
                "REQPILOT_ENV": "local",
                "REQPILOT_OBJECT_STORE": "oss",
                "REQPILOT_OBJECT_ROOT": "/tmp/inherited-objects",
                "REQPILOT_SCRATCH_ROOT": "/tmp/inherited-scratch",
                "REQPILOT_AGENT_RUNTIME": "codex-app-server",
            },
            clear=False,
        ):
            activate_test_runtime()
            settings = RuntimeSettings.from_environment()

            self.assertEqual(settings.deployment_mode, DeploymentMode.TEST)
            self.assertEqual(settings.database_backend, DatabaseBackend.SQLITE_TEST)
            self.assertEqual(settings.object_store_backend, ObjectStoreBackend.LOCAL)
            self.assertEqual(settings.object_root, storage_dir / "objects")
            self.assertEqual(settings.scratch_root, storage_dir / "scratch")
            self.assertEqual(settings.agent_runtime_backend, AgentRuntimeBackend.NONE)


if __name__ == "__main__":
    unittest.main()
