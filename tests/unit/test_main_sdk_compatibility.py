"""新版 SDK 下的離線進入點／DevUI API 驗證，不啟動模型或 Azure 服務。"""

import inspect
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent_framework_devui import serve

import main
from config import EnvironmentSettings
from test_handoff_responses import ScriptedRoleClients
from workflows.handoff import (
    WORKFLOW_ENTITY_NAME,
    WORKFLOW_NAME,
    create_devui_entities,
)


class MainSdkCompatibilityTests(unittest.TestCase):
    def test_responses_entry_keeps_local_and_hosted_credentials(self):
        settings = EnvironmentSettings(
            foundry_project_endpoint="https://offline.invalid/api/projects/offline",
            model_deployment_name="offline",
            devui_port=8088,
        )
        for hosted in (False, True):
            with self.subTest(hosted=hosted):
                args = SimpleNamespace(
                    responses=True, forward=False, host=None, port=None
                )
                with (
                    patch.object(main, "_parse_args", return_value=args),
                    patch.object(main, "load_settings_or_exit", return_value=settings),
                    patch.object(main, "create_workflow_factory") as factory,
                    patch.object(main, "_serve_responses") as response_server,
                    patch("azure.ai.agentserver.core.AgentConfig.from_env",
                          return_value=SimpleNamespace(is_hosted=hosted)),
                    patch("azure.identity.aio.AzureCliCredential") as local,
                    patch("azure.identity.aio.ManagedIdentityCredential") as managed,
                    patch.dict(os.environ, {
                        "PORT": "8088",
                        "FOUNDRY_AGENT_INSTANCE_CLIENT_ID": "synthetic-client",
                    }),
                ):
                    main.main()
                if hosted:
                    managed.assert_called_once_with(client_id="synthetic-client")
                    local.assert_not_called()
                    credential = managed.return_value
                else:
                    local.assert_called_once_with()
                    managed.assert_not_called()
                    credential = local.return_value
                factory.assert_called_once_with(
                    settings.foundry_project_endpoint, "offline", credential
                )
                response_server.assert_called_once_with(
                    factory.return_value, "127.0.0.1", 8088
                )

    def test_devui_entities_and_public_serve_contract(self):
        source = ScriptedRoleClients({})
        with (
            patch("workflows.handoff._create_client", return_value=source),
            patch.dict(os.environ, {"AGENT_NAME_PREFIX": "qvn-"}),
        ):
            agent, workflow = create_devui_entities(
                "https://offline.invalid/api/projects/offline", "offline", object()
            )
        self.assertEqual(agent.name, WORKFLOW_NAME)
        self.assertEqual(workflow.name, WORKFLOW_ENTITY_NAME)
        self.assertIsNot(agent.workflow, workflow)
        self.assertEqual(workflow.input_types, [str])
        self.assertEqual(
            {executor.id for executor in workflow.get_executors_list()},
            {"user-input", "qvn-primary-agent", "qvn-coding-agent",
             "qvn-architect-agent", "qvn-spec-agent"},
        )
        self.assertTrue(all(client.calls == 0 for client in source.clients.values()))
        binding = inspect.signature(serve).bind(
            entities=[agent, workflow],
            port=8080,
            host="127.0.0.1",
            auth_enabled=False,
            auth_token=None,
            auto_open=False,
        )
        self.assertEqual(binding.arguments["entities"], [agent, workflow])
