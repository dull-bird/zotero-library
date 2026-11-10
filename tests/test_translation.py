"""Offline translation-preference tests. No real models, CLIs or credentials."""
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from zotero_core import CuratorError, save_json
from zotero_library import main
from zotero_translation import (DEFAULTS, config_path, configure_translation, load_config,
                                show_config, translation_route, validate_config)


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'state'

    def error_code(self, code, function, *args, **kwargs):
        with self.assertRaises(CuratorError) as raised:
            function(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)

    def test_fresh_install_is_unselected_without_writing_a_file(self):
        shown = show_config(self.state)
        self.assertTrue(shown['needs_user_choice'])
        self.assertIsNone(shown['config']['translation']['mode'])
        self.assertIn('CLI', shown['question'])
        self.assertFalse(config_path(self.state).exists())

    def test_unselected_route_requires_a_user_choice_not_current_agent_fallback(self):
        with mock.patch.dict(os.environ, {'PI_PROVIDER': 'fixture', 'PI_MODEL': 'fixture-model'}):
            self.error_code('TRANSLATION_CHOICE_REQUIRED', translation_route, self.state)

    def test_agent_mode_uses_the_current_session_selection(self):
        configure_translation('agent', self.state)
        with mock.patch.dict(os.environ, {'PI_PROVIDER': 'fixture-provider', 'PI_MODEL': 'fixture-model'}):
            route = translation_route(self.state)
        self.assertEqual(route['selected_model'], {'provider': 'fixture-provider', 'model_id': 'fixture-model'})
        self.assertEqual(route['action'], 'translate_in_current_agent')
        self.assertFalse(route['executed'])

    def test_non_pi_agent_model_is_not_invented(self):
        configure_translation('agent', self.state)
        with mock.patch.dict(os.environ, {}, clear=True):
            route = translation_route(self.state)
        self.assertIsNone(route['selected_model'])
        self.assertEqual(route['model_evidence'], 'ask the calling harness')

    def test_agent_mode_follows_later_session_model_changes(self):
        configure_translation('agent', self.state)
        with mock.patch.dict(os.environ, {'PI_PROVIDER': 'fixture', 'PI_MODEL': 'model-a'}):
            self.assertEqual(translation_route(self.state)['selected_model']['model_id'], 'model-a')
        with mock.patch.dict(os.environ, {'PI_PROVIDER': 'fixture', 'PI_MODEL': 'model-b'}):
            self.assertEqual(translation_route(self.state)['selected_model']['model_id'], 'model-b')

    def test_cli_argv_round_trip(self):
        argv = ['example-translator', '--model', 'fixture-model', '--print']
        configure_translation('cli', self.state, argv=argv, label='Example CLI')
        with mock.patch('zotero_translation.shutil.which', return_value='/example/bin/example-translator'):
            route = translation_route(self.state)
        self.assertEqual(route['argv'], argv)
        self.assertFalse(route['executed'])
        self.assertFalse(route['credentials_verified'])
        self.assertFalse(route['model_call_verified'])
        self.assertIsNone(route['fallback'])

    def test_command_string_is_split_not_executed(self):
        sentinel = Path(self.tmp.name) / 'must-not-be-created'
        command = 'example-translator --model "model with spaces" "$(touch ' + str(sentinel) + ')"'
        configure_translation('cli', self.state, command=command)
        with mock.patch('zotero_translation.shutil.which', return_value='/example/bin/example-translator'):
            route = translation_route(self.state)
        self.assertEqual(route['argv'][2], 'model with spaces')
        self.assertEqual(route['argv'][3], '$(touch ' + str(sentinel) + ')')
        self.assertFalse(sentinel.exists())

    def test_missing_cli_stops_without_changing_the_preference(self):
        configure_translation('cli', self.state, argv=['missing-example-translator'])
        with mock.patch('zotero_translation.shutil.which', return_value=None):
            self.error_code('TRANSLATION_CLI_NOT_FOUND', translation_route, self.state)
        self.assertEqual(load_config(self.state)['translation']['mode'], 'cli')

    def test_cli_mode_requires_a_command(self):
        self.error_code('INVALID_TRANSLATION_CONFIG', configure_translation, 'cli', self.state)
        self.assertFalse(config_path(self.state).exists())

    def test_agent_mode_rejects_cli_arguments(self):
        self.error_code('INVALID_TRANSLATION_CONFIG', configure_translation, 'agent', self.state,
                        argv=['example-translator'])

    def test_two_command_formats_cannot_be_mixed(self):
        self.error_code('INVALID_TRANSLATION_CONFIG', configure_translation, 'cli', self.state,
                        argv=['example-translator'], command='another-translator')

    def test_invalid_command_quoting_is_not_saved(self):
        self.error_code('INVALID_TRANSLATION_CONFIG', configure_translation, 'cli', self.state,
                        command='example-translator "unclosed')

    def test_cli_argv_must_be_a_string_array(self):
        for argv in ('example-translator', [], [3], [''], ['--model'], ['example', 'bad\0argument']):
            with self.subTest(argv=argv):
                self.error_code('INVALID_TRANSLATION_CONFIG', configure_translation, 'cli', self.state, argv=argv)

    def test_common_credential_flags_are_not_saved_or_echoed(self):
        for flag in ('--api-key', '--api_key=FAKE', '--token', '--password', 'Authorization: Bearer FAKE'):
            with self.subTest(flag=flag):
                self.error_code('CREDENTIAL_IN_TRANSLATION_CONFIG', configure_translation, 'cli', self.state,
                                argv=['example-translator', flag, 'NOT-A-REAL-CREDENTIAL'])
        self.assertFalse(config_path(self.state).exists())

    def test_label_is_not_a_place_to_store_credentials(self):
        self.error_code('CREDENTIAL_IN_TRANSLATION_CONFIG', configure_translation, 'cli', self.state,
                        argv=['example-translator'], label='Authorization: Bearer NOT-A-REAL-CREDENTIAL')

    def test_configuration_is_owner_only(self):
        configure_translation('agent', self.state)
        self.assertEqual(stat.S_IMODE(config_path(self.state).stat().st_mode), 0o600)

    def test_insecure_configuration_is_not_read(self):
        configure_translation('agent', self.state)
        config_path(self.state).chmod(0o644)
        self.error_code('UNSAFE_TRANSLATION_CONFIG', load_config, self.state)

    def test_symlink_configuration_is_not_followed_or_overwritten(self):
        self.state.mkdir()
        target = Path(self.tmp.name) / 'external.json'
        save_json(target, DEFAULTS)
        config_path(self.state).symlink_to(target)
        self.error_code('UNSAFE_TRANSLATION_CONFIG', load_config, self.state)
        self.error_code('UNSAFE_TRANSLATION_CONFIG', configure_translation, 'agent', self.state)
        self.assertEqual(json.loads(target.read_text()), DEFAULTS)

    def test_invalid_existing_config_is_not_silently_replaced(self):
        save_json(config_path(self.state), {'format': 999, 'translation': {'mode': 'agent'}})
        self.error_code('INVALID_TRANSLATION_CONFIG', configure_translation, 'agent', self.state)
        self.assertEqual(json.loads(config_path(self.state).read_text())['format'], 999)

    def test_unknown_modes_and_fields_are_rejected(self):
        for translation in ({'mode': 'auto'}, {'mode': 'agent', 'cli': {'argv': ['example']}},
                            {'mode': 'agent', 'fallback': 'some-model'}):
            with self.subTest(translation=translation):
                self.error_code('INVALID_TRANSLATION_CONFIG', validate_config,
                                {'format': 1, 'translation': translation})

    def test_unknown_root_fields_and_boolean_format_are_rejected(self):
        for config in ({'format': True, 'translation': {'mode': None}},
                       {'format': 1, 'translation': {'mode': None}, 'unrecognized': 'field'}):
            with self.subTest(config=config):
                self.error_code('INVALID_TRANSLATION_CONFIG', validate_config, config)

    def test_reset_requires_choice_next_time_and_clears_stale_cli(self):
        configure_translation('cli', self.state, argv=['example-translator'])
        shown = configure_translation(None, self.state)
        self.assertTrue(shown['needs_user_choice'])
        self.assertIsNone(shown['config']['translation']['cli'])
        self.error_code('TRANSLATION_CHOICE_REQUIRED', translation_route, self.state)

    def test_read_does_not_mutate_defaults(self):
        config = load_config(self.state)
        config['translation']['mode'] = 'agent'
        self.assertEqual(load_config(self.state), DEFAULTS)
        self.assertIsNone(DEFAULTS['translation']['mode'])

    def test_cli_commands_do_not_require_zotero_or_its_authorization(self):
        with mock.patch('zotero_library.LocalClient') as client:
            shown = main(['--state', str(self.state), 'translation', 'show'])
            self.assertTrue(shown['needs_user_choice'])
            client.assert_not_called()

    def test_cli_configure_and_route(self):
        main(['--state', str(self.state), 'translation', 'set', '--mode', 'cli',
              '--argv-json', '["example-translator","--print"]'])
        with mock.patch('zotero_translation.shutil.which', return_value='/example/bin/example-translator'):
            route = main(['--state', str(self.state), 'translation', 'route'])
        self.assertEqual(route['argv'], ['example-translator', '--print'])
        shown = main(['--state', str(self.state), 'translation', 'reset'])
        self.assertTrue(shown['needs_user_choice'])

    def test_generic_example_has_no_default_model_or_cli(self):
        example = Path(__file__).resolve().parents[1] / 'config.example.json'
        self.assertEqual(validate_config(json.loads(example.read_text())), DEFAULTS)


if __name__ == '__main__':
    unittest.main()
