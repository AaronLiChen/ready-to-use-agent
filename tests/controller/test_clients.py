import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import clients


class ClientIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.native=self.root/'cursor/v1/agent'
        self.native.parent.mkdir(parents=True)
        self.native.write_text('fixture')
        self.script=self.root/'kimi/cli.js'
        self.script.parent.mkdir()
        self.script.write_text('fixture')
        self.alias=self.root/'bin/agent'
        self.alias.parent.mkdir()
        self.alias.symlink_to(self.native)
        self.rows=[{'kind':'cursor','executables':[str(self.alias)],'install_roots':[str(self.root/'cursor')],'native_names':['agent']},
                   {'kind':'kimi','executables':[str(self.script)],'install_roots':[],'native_names':['kimi']}]

    def test_native_symlink_resolves_to_installed_frontend(self):
        self.assertTrue(clients.harness_matches([str(self.native),'--help'],self.rows))

    def test_interpreter_matches_the_entrypoint_not_the_prompt(self):
        self.assertTrue(clients.harness_matches(['/usr/bin/node',str(self.script),'-p','fix task'],self.rows))
        self.assertTrue(clients.harness_matches(['/usr/bin/node','--no-warnings',str(self.script)],self.rows))

    def test_native_update_within_owned_install_prefix_is_allowed(self):
        self.assertTrue(clients.harness_matches([str(self.root/'cursor/v2/agent')],self.rows))

    def test_same_basename_outside_the_install_prefix_is_not_allowed(self):
        self.assertFalse(clients.harness_matches([str(self.root/'other/agent')],self.rows))

    def test_shell_goal_text_is_not_a_frontend(self):
        self.assertFalse(clients.harness_matches(['/bin/bash','-c',str(self.native)],self.rows))

    def test_node_eval_arguments_are_not_an_entrypoint(self):
        self.assertFalse(clients.harness_matches(['/usr/bin/node','--eval',str(self.script)],self.rows))

    def test_missing_or_malformed_registry_is_safe(self):
        with patch.object(clients,'REGISTRY',self.root/'missing'):
            self.assertEqual(clients.installed_clients(),[])
        path=self.root/'bad.json';path.write_text('{')
        with patch.object(clients,'REGISTRY',path):
            self.assertEqual(clients.installed_clients(),[])
        path.write_text(json.dumps({'version':2,'clients':self.rows}))
        with patch.object(clients,'REGISTRY',path):
            self.assertEqual(clients.installed_clients(),[])

    def test_installed_frontend_can_own_a_foreground_job(self):
        from types import SimpleNamespace
        import workflow
        responses=[SimpleNamespace(stdout='777 /bin/bash -c agent-workflow start --goal review'),
                   SimpleNamespace(stdout='1 '+str(self.native))]
        with patch.object(clients,'installed_clients',return_value=self.rows), \
             patch.object(workflow.subprocess,'run',side_effect=responses), \
             patch.object(workflow,'process_identity',return_value='started'):
            self.assertEqual(workflow.owner(),{'pid':777,'identity':'started'})


if __name__=='__main__':unittest.main()
