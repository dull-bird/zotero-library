"""User-selected translation routing; never invokes a model, CLI or shell itself."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import re
import shlex
import shutil

from zotero_core import CuratorError, STATE, load_json, save_json

DEFAULTS = {'format': 1, 'translation': {'mode': None, 'cli': None}}
CHOICE_QUESTION = ('论文翻译默认使用当前 agent 的模型，还是你指定的 CLI？'
                   '如果选 CLI，请提供程序和参数；此选择只用于翻译，不改变聊天或编程模型。')


def config_path(state=STATE):
    return Path(state).expanduser() / 'config.json'


def _validate_cli(cli):
    if not isinstance(cli, dict) or set(cli) - {'argv', 'label'}:
        raise CuratorError('CLI configuration must contain argv and an optional label', 'INVALID_TRANSLATION_CONFIG')
    argv = cli.get('argv')
    if (not isinstance(argv, list) or not argv or len(argv) > 128
            or any(not isinstance(a, str) or '\0' in a for a in argv)
            or not argv[0].strip() or argv[0].startswith('-')):
        raise CuratorError('CLI argv must be a nonempty string array without NUL bytes', 'INVALID_TRANSLATION_CONFIG')
    label = cli.get('label')
    if label is not None and (not isinstance(label, str) or not label.strip() or len(label) > 200):
        raise CuratorError('CLI label must be a short nonempty string', 'INVALID_TRANSLATION_CONFIG')
    # Authentication belongs to the selected CLI's own login/environment, not printable preferences.
    credential_flag = re.compile(r'^--(?:api[-_]key|access[-_]token|bearer[-_]token|token|password)(?:=|$)', re.I)
    secret = re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-(?:proj-)?[A-Za-z0-9_-]{20,})')
    if any(credential_flag.search(a) or re.search(r'authorization\s*:', a, re.I) or secret.search(a)
           for a in argv + ([label] if label else [])):
        raise CuratorError('Keep credentials in the CLI login store or environment, not translation configuration',
                           'CREDENTIAL_IN_TRANSLATION_CONFIG')


def validate_config(config):
    if (not isinstance(config, dict) or type(config.get('format')) is not int or config['format'] != 1
            or set(config) - {'format', 'translation'}):
        raise CuratorError('Unsupported translation configuration format or fields', 'INVALID_TRANSLATION_CONFIG')
    translation = config.get('translation')
    if not isinstance(translation, dict) or set(translation) - {'mode', 'cli'}:
        raise CuratorError('Invalid translation configuration fields', 'INVALID_TRANSLATION_CONFIG')
    mode = translation.get('mode')
    if mode not in (None, 'agent', 'cli'):
        raise CuratorError('Translation mode must be null, agent or cli', 'INVALID_TRANSLATION_CONFIG')
    if mode == 'cli':
        _validate_cli(translation.get('cli'))
    elif translation.get('cli') is not None:
        raise CuratorError('CLI arguments are only valid in cli mode', 'INVALID_TRANSLATION_CONFIG')
    return config


def load_config(state=STATE):
    path = config_path(state)
    if path.is_symlink():
        raise CuratorError('Do not load translation commands through a configuration symlink', 'UNSAFE_TRANSLATION_CONFIG')
    if not path.exists():
        return copy.deepcopy(DEFAULTS)
    stat = path.stat()
    if stat.st_mode & 0o077 or (hasattr(os, 'getuid') and stat.st_uid != os.getuid()):
        raise CuratorError('Translation config must be owned by the current user and chmod 600',
                           'UNSAFE_TRANSLATION_CONFIG')
    return validate_config(load_json(path))


def configure_translation(mode, state=STATE, argv=None, command=None, label=None):
    if argv is not None and command is not None:
        raise CuratorError('Choose either argv JSON or a command string', 'INVALID_TRANSLATION_CONFIG')
    if mode not in (None, 'agent', 'cli'):
        raise CuratorError('Unknown translation mode', 'INVALID_TRANSLATION_CONFIG')
    if command is not None:
        try:
            argv = shlex.split(command)
        except ValueError:
            raise CuratorError('Invalid CLI quoting', 'INVALID_TRANSLATION_CONFIG')
    if mode != 'cli' and (argv is not None or label is not None):
        raise CuratorError('Only cli mode accepts a command or label', 'INVALID_TRANSLATION_CONFIG')
    config = load_config(state)
    config['translation'] = {'mode': mode, 'cli': {'argv': argv, 'label': label} if mode == 'cli' else None}
    validate_config(config)
    save_json(config_path(state), config)
    return show_config(state)


def show_config(state=STATE):
    config = load_config(state)
    return {'file': str(config_path(state)), 'config': config,
            'needs_user_choice': config['translation']['mode'] is None,
            'question': CHOICE_QUESTION if config['translation']['mode'] is None else None}


def translation_route(state=STATE):
    """Return a plan for the calling agent, not a fabricated translation result."""
    translation = load_config(state)['translation']
    if translation['mode'] is None:
        raise CuratorError(CHOICE_QUESTION, 'TRANSLATION_CHOICE_REQUIRED')
    if translation['mode'] == 'agent':
        provider, model = os.environ.get('PI_PROVIDER'), os.environ.get('PI_MODEL')
        return {'mode': 'agent', 'action': 'translate_in_current_agent',
                'selected_model': {'provider': provider, 'model_id': model} if provider and model else None,
                'model_evidence': 'Pi shell session environment' if provider and model else 'ask the calling harness',
                'executed': False}
    cli = translation['cli']
    executable = shutil.which(cli['argv'][0])
    if executable is None:
        raise CuratorError('Selected translation CLI is not installed or executable; do not fall back to another model',
                           'TRANSLATION_CLI_NOT_FOUND')
    return {'mode': 'cli', 'action': 'invoke_user_selected_cli', 'argv': copy.deepcopy(cli['argv']),
            'label': cli.get('label'), 'executable': executable,
            'input_contract': 'translation prompt on stdin', 'output_contract': 'final translated text on stdout',
            'credentials_verified': False, 'model_call_verified': False,
            'executed': False, 'fallback': None}
