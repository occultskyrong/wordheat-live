"""Consume trusted-runner evidence; never execute manifest commands or shell text."""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

SPEC_SHA256 = '6c582420ea7d80d66cea1178e8629e7358a6a23a88c1d5ea65843a47f821f63b'
SPEC_PATH = 'docs/specs/20261025 - 直播语义猜词 - 后端开发Spec.md'
# Version 2 binds BE-A19 to its exact cross-module selection dependencies.
BOUNDARY_VERSION = 2
A19_DEPENDENCY_PATHS = [
    'apps/api/app/modules/wordbank/selection.py',
    'apps/api/app/modules/wordbank/catalog.py',
    'apps/api/app/modules/wordbank/service.py',
    'apps/api/app/modules/rounds/worker.py',
]
# Fixed reviewable tool baseline. Manifest edits cannot change ownership or required environments.
BOUNDARIES = {'BE-01': {'environment': 'postgres',
           'implementation_prefixes': ['apps/api/app/modules/inbox/'],
           'owner': 'api.inbox',
           'test_prefixes': ['apps/api/tests/unit/inbox/',
                             'apps/api/tests/integration/inbox/',
                             'apps/api/tests/contracts/inbox/',
                             'apps/api/tests/replay/inbox/',
                             'tests/integration/api/inbox/',
                             'tests/windows/api/inbox/']},
 'BE-02': {'environment': 'model',
           'implementation_prefixes': ['apps/scorer/app/'],
           'owner': 'scorer',
           'test_prefixes': ['apps/scorer/tests/', 'tests/integration/scorer/', 'tests/windows/scorer/']},
 'BE-03': {'environment': 'postgres',
           'implementation_prefixes': ['apps/api/app/modules/rounds/'],
           'owner': 'api.rounds',
           'test_prefixes': ['apps/api/tests/unit/rounds/',
                             'apps/api/tests/integration/rounds/',
                             'apps/api/tests/contracts/rounds/',
                             'apps/api/tests/replay/rounds/',
                             'tests/integration/api/rounds/',
                             'tests/windows/api/rounds/']},
 'BE-04': {'environment': 'local',
           'implementation_prefixes': ['apps/api/app/modules/auth/'],
           'owner': 'api.auth',
           'test_prefixes': ['apps/api/tests/unit/auth/',
                             'apps/api/tests/integration/auth/',
                             'apps/api/tests/contracts/auth/',
                             'apps/api/tests/replay/auth/',
                             'tests/integration/api/auth/',
                             'tests/windows/api/auth/']},
 'BE-05': {'environment': 'windows_obs',
           'implementation_prefixes': ['apps/api/app/modules/realtime/'],
           'owner': 'api.realtime',
           'test_prefixes': ['apps/api/tests/unit/realtime/',
                             'apps/api/tests/integration/realtime/',
                             'apps/api/tests/contracts/realtime/',
                             'apps/api/tests/replay/realtime/',
                             'tests/integration/api/realtime/',
                             'tests/windows/api/realtime/']},
 'BE-06': {'environment': 'relay_postgres',
           'implementation_prefixes': ['apps/relay/app/'],
           'owner': 'relay',
           'test_prefixes': ['apps/relay/tests/', 'tests/integration/relay/', 'tests/windows/relay/']},
 'BE-07': {'environment': 'relay_postgres',
           'implementation_prefixes': ['apps/relay/app/'],
           'owner': 'relay',
           'test_prefixes': ['apps/relay/tests/', 'tests/integration/relay/', 'tests/windows/relay/']},
 'BE-08': {'environment': 'relay_postgres',
           'implementation_prefixes': ['apps/relay/app/'],
           'owner': 'relay',
           'test_prefixes': ['apps/relay/tests/', 'tests/integration/relay/', 'tests/windows/relay/']},
 'BE-09': {'environment': 'windows',
           'implementation_prefixes': ['apps/windows-launcher/', 'deploy/windows-native/'],
           'owner': 'windows-launcher',
           'test_prefixes': ['apps/windows-launcher/tests/', 'tests/windows/launcher/']},
 'BE-10': {'environment': 'postgres',
           'implementation_prefixes': ['apps/api/app/modules/analytics/'],
           'owner': 'api.analytics',
           'test_prefixes': ['apps/api/tests/unit/analytics/',
                             'apps/api/tests/integration/analytics/',
                             'apps/api/tests/contracts/analytics/',
                             'apps/api/tests/replay/analytics/',
                             'tests/integration/api/analytics/',
                             'tests/windows/api/analytics/']},
 'BE-A01': {'environment': 'postgres',
            'implementation_prefixes': ['apps/api/app/modules/inbox/'],
            'owner': 'api.inbox',
            'test_prefixes': ['apps/api/tests/unit/inbox/',
                              'apps/api/tests/integration/inbox/',
                              'apps/api/tests/contracts/inbox/',
                              'apps/api/tests/replay/inbox/',
                              'tests/integration/api/inbox/',
                              'tests/windows/api/inbox/']},
 'BE-A02': {'environment': 'model',
            'implementation_prefixes': ['apps/scorer/app/'],
            'owner': 'scorer',
            'test_prefixes': ['apps/scorer/tests/', 'tests/integration/scorer/', 'tests/windows/scorer/']},
 'BE-A03': {'environment': 'postgres',
            'implementation_prefixes': ['apps/api/app/modules/rounds/'],
            'owner': 'api.rounds',
            'test_prefixes': ['apps/api/tests/unit/rounds/',
                              'apps/api/tests/integration/rounds/',
                              'apps/api/tests/contracts/rounds/',
                              'apps/api/tests/replay/rounds/',
                              'tests/integration/api/rounds/',
                              'tests/windows/api/rounds/']},
 'BE-A04': {'environment': 'local',
            'implementation_prefixes': ['apps/api/app/modules/auth/'],
            'owner': 'api.auth',
            'test_prefixes': ['apps/api/tests/unit/auth/',
                              'apps/api/tests/integration/auth/',
                              'apps/api/tests/contracts/auth/',
                              'apps/api/tests/replay/auth/',
                              'tests/integration/api/auth/',
                              'tests/windows/api/auth/']},
 'BE-A05': {'environment': 'windows_obs',
            'implementation_prefixes': ['apps/api/app/modules/realtime/'],
            'owner': 'api.realtime',
            'test_prefixes': ['apps/api/tests/unit/realtime/',
                              'apps/api/tests/integration/realtime/',
                              'apps/api/tests/contracts/realtime/',
                              'apps/api/tests/replay/realtime/',
                              'tests/integration/api/realtime/',
                              'tests/windows/api/realtime/']},
 'BE-A06': {'environment': 'relay_postgres',
            'implementation_prefixes': ['apps/relay/app/'],
            'owner': 'relay',
            'test_prefixes': ['apps/relay/tests/', 'tests/integration/relay/', 'tests/windows/relay/']},
 'BE-A07': {'environment': 'relay_postgres',
            'implementation_prefixes': ['apps/relay/app/'],
            'owner': 'relay',
            'test_prefixes': ['apps/relay/tests/', 'tests/integration/relay/', 'tests/windows/relay/']},
 'BE-A08': {'environment': 'relay_postgres',
            'implementation_prefixes': ['apps/relay/app/'],
            'owner': 'relay',
            'test_prefixes': ['apps/relay/tests/', 'tests/integration/relay/', 'tests/windows/relay/']},
 'BE-A09': {'environment': 'postgres_model',
            'implementation_prefixes': ['apps/api/app/modules/wordbank/'],
            'owner': 'api.wordbank',
            'test_prefixes': ['apps/api/tests/unit/wordbank/',
                              'apps/api/tests/integration/wordbank/',
                              'apps/api/tests/contracts/wordbank/',
                              'apps/api/tests/replay/wordbank/',
                              'tests/integration/api/wordbank/',
                              'tests/windows/api/wordbank/']},
 'BE-A10': {'environment': 'windows',
            'implementation_prefixes': ['apps/windows-launcher/', 'deploy/windows-native/'],
            'owner': 'windows-launcher',
            'test_prefixes': ['apps/windows-launcher/tests/', 'tests/windows/launcher/']},
 'BE-A11': {'environment': 'windows',
            'implementation_prefixes': ['apps/windows-launcher/', 'deploy/windows-native/'],
            'owner': 'windows-launcher',
            'test_prefixes': ['apps/windows-launcher/tests/', 'tests/windows/launcher/']},
 'BE-A12': {'environment': 'platform',
            'implementation_prefixes': ['apps/relay/app/'],
            'owner': 'relay',
            'test_prefixes': ['apps/relay/tests/', 'tests/integration/relay/', 'tests/windows/relay/']},
 'BE-A13': {'environment': 'windows_obs',
            'implementation_prefixes': ['apps/api/app/modules/realtime/'],
            'owner': 'api.realtime',
            'test_prefixes': ['apps/api/tests/unit/realtime/',
                              'apps/api/tests/integration/realtime/',
                              'apps/api/tests/contracts/realtime/',
                              'apps/api/tests/replay/realtime/',
                              'tests/integration/api/realtime/',
                              'tests/windows/api/realtime/']},
 'BE-A14': {'environment': 'platform',
            'implementation_prefixes': ['apps/api/app/modules/operations/'],
            'owner': 'api.operations',
            'test_prefixes': ['apps/api/tests/unit/operations/',
                              'apps/api/tests/integration/operations/',
                              'apps/api/tests/contracts/operations/',
                              'apps/api/tests/replay/operations/',
                              'tests/integration/api/operations/',
                              'tests/windows/api/operations/']},
 'BE-A15': {'environment': 'postgres',
            'implementation_prefixes': ['apps/api/app/modules/analytics/'],
            'owner': 'api.analytics',
            'test_prefixes': ['apps/api/tests/unit/analytics/',
                              'apps/api/tests/integration/analytics/',
                              'apps/api/tests/contracts/analytics/',
                              'apps/api/tests/replay/analytics/',
                              'tests/integration/api/analytics/',
                              'tests/windows/api/analytics/']},
 'BE-A16': {'environment': 'postgres',
            'implementation_prefixes': ['apps/api/app/modules/sessions/'],
            'owner': 'api.sessions',
            'test_prefixes': ['apps/api/tests/unit/sessions/',
                              'apps/api/tests/integration/sessions/',
                              'apps/api/tests/contracts/sessions/',
                              'apps/api/tests/replay/sessions/',
                              'tests/integration/api/sessions/',
                              'tests/windows/api/sessions/']},
 'BE-A17': {'environment': 'windows',
            'implementation_prefixes': ['apps/api/app/modules/rounds/'],
            'owner': 'api.rounds',
            'test_prefixes': ['apps/api/tests/unit/rounds/',
                              'apps/api/tests/integration/rounds/',
                              'apps/api/tests/contracts/rounds/',
                              'apps/api/tests/replay/rounds/',
                              'tests/integration/api/rounds/',
                              'tests/windows/api/rounds/']},
 'BE-A18': {'environment': 'platform',
            'implementation_prefixes': ['apps/api/app/modules/operations/'],
            'owner': 'api.operations',
            'test_prefixes': ['apps/api/tests/unit/operations/',
                              'apps/api/tests/integration/operations/',
                              'apps/api/tests/contracts/operations/',
                              'apps/api/tests/replay/operations/',
                              'tests/integration/api/operations/',
                              'tests/windows/api/operations/']},
 'BE-A19': {'environment': 'postgres',
            'implementation_prefixes': ['apps/api/app/modules/sessions/', *A19_DEPENDENCY_PATHS],
            'required_implementation_paths': A19_DEPENDENCY_PATHS,
            'owner': 'api.sessions',
            'test_prefixes': ['apps/api/tests/unit/sessions/',
                              'apps/api/tests/integration/sessions/',
                              'apps/api/tests/contracts/sessions/',
                              'apps/api/tests/replay/sessions/',
                              'tests/integration/api/sessions/',
                              'tests/windows/api/sessions/']},
 'BE-A20': {'environment': 'postgres',
            'implementation_prefixes': ['apps/api/app/modules/analytics/'],
            'owner': 'api.analytics',
            'test_prefixes': ['apps/api/tests/unit/analytics/',
                              'apps/api/tests/integration/analytics/',
                              'apps/api/tests/contracts/analytics/',
                              'apps/api/tests/replay/analytics/',
                              'tests/integration/api/analytics/',
                              'tests/windows/api/analytics/']},
 'BE-A21': {'environment': 'windows_obs',
            'implementation_prefixes': ['apps/api/app/modules/analytics/'],
            'owner': 'api.analytics',
            'test_prefixes': ['apps/api/tests/unit/analytics/',
                              'apps/api/tests/integration/analytics/',
                              'apps/api/tests/contracts/analytics/',
                              'apps/api/tests/replay/analytics/',
                              'tests/integration/api/analytics/',
                              'tests/windows/api/analytics/']},
 'BOUNDARY-api.analytics': {'environment': 'local',
                            'implementation_prefixes': ['apps/api/app/modules/analytics/'],
                            'owner': 'api.analytics',
                            'test_prefixes': ['apps/api/tests/unit/analytics/',
                                              'apps/api/tests/integration/analytics/',
                                              'apps/api/tests/contracts/analytics/',
                                              'apps/api/tests/replay/analytics/',
                                              'tests/integration/api/analytics/',
                                              'tests/windows/api/analytics/']},
 'BOUNDARY-api.auth': {'environment': 'local',
                       'implementation_prefixes': ['apps/api/app/modules/auth/'],
                       'owner': 'api.auth',
                       'test_prefixes': ['apps/api/tests/unit/auth/',
                                         'apps/api/tests/integration/auth/',
                                         'apps/api/tests/contracts/auth/',
                                         'apps/api/tests/replay/auth/',
                                         'tests/integration/api/auth/',
                                         'tests/windows/api/auth/']},
 'BOUNDARY-api.connectors': {'environment': 'local',
                             'implementation_prefixes': ['apps/api/app/modules/connectors/'],
                             'owner': 'api.connectors',
                             'test_prefixes': ['apps/api/tests/unit/connectors/',
                                               'apps/api/tests/integration/connectors/',
                                               'apps/api/tests/contracts/connectors/',
                                               'apps/api/tests/replay/connectors/',
                                               'tests/integration/api/connectors/',
                                               'tests/windows/api/connectors/']},
 'BOUNDARY-api.inbox': {'environment': 'local',
                        'implementation_prefixes': ['apps/api/app/modules/inbox/'],
                        'owner': 'api.inbox',
                        'test_prefixes': ['apps/api/tests/unit/inbox/',
                                          'apps/api/tests/integration/inbox/',
                                          'apps/api/tests/contracts/inbox/',
                                          'apps/api/tests/replay/inbox/',
                                          'tests/integration/api/inbox/',
                                          'tests/windows/api/inbox/']},
 'BOUNDARY-api.operations': {'environment': 'local',
                             'implementation_prefixes': ['apps/api/app/modules/operations/'],
                             'owner': 'api.operations',
                             'test_prefixes': ['apps/api/tests/unit/operations/',
                                               'apps/api/tests/integration/operations/',
                                               'apps/api/tests/contracts/operations/',
                                               'apps/api/tests/replay/operations/',
                                               'tests/integration/api/operations/',
                                               'tests/windows/api/operations/']},
 'BOUNDARY-api.ranking': {'environment': 'local',
                          'implementation_prefixes': ['apps/api/app/modules/ranking/'],
                          'owner': 'api.ranking',
                          'test_prefixes': ['apps/api/tests/unit/ranking/',
                                            'apps/api/tests/integration/ranking/',
                                            'apps/api/tests/contracts/ranking/',
                                            'apps/api/tests/replay/ranking/',
                                            'tests/integration/api/ranking/',
                                            'tests/windows/api/ranking/']},
 'BOUNDARY-api.realtime': {'environment': 'local',
                           'implementation_prefixes': ['apps/api/app/modules/realtime/'],
                           'owner': 'api.realtime',
                           'test_prefixes': ['apps/api/tests/unit/realtime/',
                                             'apps/api/tests/integration/realtime/',
                                             'apps/api/tests/contracts/realtime/',
                                             'apps/api/tests/replay/realtime/',
                                             'tests/integration/api/realtime/',
                                             'tests/windows/api/realtime/']},
 'BOUNDARY-api.replays': {'environment': 'local',
                          'implementation_prefixes': ['apps/api/app/modules/replays/'],
                          'owner': 'api.replays',
                          'test_prefixes': ['apps/api/tests/unit/replays/',
                                            'apps/api/tests/integration/replays/',
                                            'apps/api/tests/contracts/replays/',
                                            'apps/api/tests/replay/replays/',
                                            'tests/integration/api/replays/',
                                            'tests/windows/api/replays/']},
 'BOUNDARY-api.rounds': {'environment': 'local',
                         'implementation_prefixes': ['apps/api/app/modules/rounds/'],
                         'owner': 'api.rounds',
                         'test_prefixes': ['apps/api/tests/unit/rounds/',
                                           'apps/api/tests/integration/rounds/',
                                           'apps/api/tests/contracts/rounds/',
                                           'apps/api/tests/replay/rounds/',
                                           'tests/integration/api/rounds/',
                                           'tests/windows/api/rounds/']},
 'BOUNDARY-api.scoring': {'environment': 'local',
                          'implementation_prefixes': ['apps/api/app/modules/scoring/'],
                          'owner': 'api.scoring',
                          'test_prefixes': ['apps/api/tests/unit/scoring/',
                                            'apps/api/tests/integration/scoring/',
                                            'apps/api/tests/contracts/scoring/',
                                            'apps/api/tests/replay/scoring/',
                                            'tests/integration/api/scoring/',
                                            'tests/windows/api/scoring/']},
 'BOUNDARY-api.sessions': {'environment': 'local',
                           'implementation_prefixes': ['apps/api/app/modules/sessions/'],
                           'owner': 'api.sessions',
                           'test_prefixes': ['apps/api/tests/unit/sessions/',
                                             'apps/api/tests/integration/sessions/',
                                             'apps/api/tests/contracts/sessions/',
                                             'apps/api/tests/replay/sessions/',
                                             'tests/integration/api/sessions/',
                                             'tests/windows/api/sessions/']},
 'BOUNDARY-api.wordbank': {'environment': 'local',
                           'implementation_prefixes': ['apps/api/app/modules/wordbank/'],
                           'owner': 'api.wordbank',
                           'test_prefixes': ['apps/api/tests/unit/wordbank/',
                                             'apps/api/tests/integration/wordbank/',
                                             'apps/api/tests/contracts/wordbank/',
                                             'apps/api/tests/replay/wordbank/',
                                             'tests/integration/api/wordbank/',
                                             'tests/windows/api/wordbank/']},
 'BOUNDARY-contracts': {'environment': 'local',
                        'implementation_prefixes': ['packages/contracts/'],
                        'owner': 'contracts',
                        'test_prefixes': ['packages/contracts/tests/']},
 'BOUNDARY-relay': {'environment': 'local',
                    'implementation_prefixes': ['apps/relay/app/'],
                    'owner': 'relay',
                    'test_prefixes': ['apps/relay/tests/',
                                      'tests/integration/relay/',
                                      'tests/windows/relay/']},
 'BOUNDARY-scorer': {'environment': 'local',
                     'implementation_prefixes': ['apps/scorer/app/'],
                     'owner': 'scorer',
                     'test_prefixes': ['apps/scorer/tests/',
                                       'tests/integration/scorer/',
                                       'tests/windows/scorer/']},
 'BOUNDARY-windows-launcher': {'environment': 'local',
                               'implementation_prefixes': ['apps/windows-launcher/',
                                                           'deploy/windows-native/'],
                               'owner': 'windows-launcher',
                               'test_prefixes': ['apps/windows-launcher/tests/', 'tests/windows/launcher/']},
 'COMPLETION-TOOL': {'environment': 'local',
                     'implementation_prefixes': ['scripts/backend/', 'docs/acceptance/backend-checks.json'],
                     'owner': 'completion',
                     'test_prefixes': ['scripts/backend/tests/']},
 'CONTRACTS-SUBSET': {'environment': 'local',
                      'implementation_prefixes': ['packages/contracts/'],
                      'owner': 'contracts',
                      'test_prefixes': ['packages/contracts/tests/']},
 'T01': {'environment': 'platform',
         'implementation_prefixes': ['apps/relay/app/'],
         'owner': 'relay',
         'test_prefixes': ['apps/relay/tests/', 'tests/integration/relay/', 'tests/windows/relay/']},
 'T02': {'environment': 'platform',
         'implementation_prefixes': ['apps/relay/app/'],
         'owner': 'relay',
         'test_prefixes': ['apps/relay/tests/', 'tests/integration/relay/', 'tests/windows/relay/']},
 'T03': {'environment': 'postgres',
         'implementation_prefixes': ['apps/api/app/modules/inbox/'],
         'owner': 'api.inbox',
         'test_prefixes': ['apps/api/tests/unit/inbox/',
                           'apps/api/tests/integration/inbox/',
                           'apps/api/tests/contracts/inbox/',
                           'apps/api/tests/replay/inbox/',
                           'tests/integration/api/inbox/',
                           'tests/windows/api/inbox/']},
 'T04': {'environment': 'postgres',
         'implementation_prefixes': ['apps/api/app/modules/inbox/'],
         'owner': 'api.inbox',
         'test_prefixes': ['apps/api/tests/unit/inbox/',
                           'apps/api/tests/integration/inbox/',
                           'apps/api/tests/contracts/inbox/',
                           'apps/api/tests/replay/inbox/',
                           'tests/integration/api/inbox/',
                           'tests/windows/api/inbox/']},
 'T05': {'environment': 'postgres',
         'implementation_prefixes': ['apps/api/app/modules/inbox/'],
         'owner': 'api.inbox',
         'test_prefixes': ['apps/api/tests/unit/inbox/',
                           'apps/api/tests/integration/inbox/',
                           'apps/api/tests/contracts/inbox/',
                           'apps/api/tests/replay/inbox/',
                           'tests/integration/api/inbox/',
                           'tests/windows/api/inbox/']},
 'T06': {'environment': 'model',
         'implementation_prefixes': ['apps/scorer/app/'],
         'owner': 'scorer',
         'test_prefixes': ['apps/scorer/tests/', 'tests/integration/scorer/', 'tests/windows/scorer/']},
 'T07': {'environment': 'model',
         'implementation_prefixes': ['apps/scorer/app/'],
         'owner': 'scorer',
         'test_prefixes': ['apps/scorer/tests/', 'tests/integration/scorer/', 'tests/windows/scorer/']},
 'T08': {'environment': 'model',
         'implementation_prefixes': ['apps/scorer/app/'],
         'owner': 'scorer',
         'test_prefixes': ['apps/scorer/tests/', 'tests/integration/scorer/', 'tests/windows/scorer/']},
 'T09': {'environment': 'postgres',
         'implementation_prefixes': ['apps/api/app/modules/rounds/'],
         'owner': 'api.rounds',
         'test_prefixes': ['apps/api/tests/unit/rounds/',
                           'apps/api/tests/integration/rounds/',
                           'apps/api/tests/contracts/rounds/',
                           'apps/api/tests/replay/rounds/',
                           'tests/integration/api/rounds/',
                           'tests/windows/api/rounds/']},
 'T10': {'environment': 'relay_postgres',
         'implementation_prefixes': ['apps/relay/app/'],
         'owner': 'relay',
         'test_prefixes': ['apps/relay/tests/', 'tests/integration/relay/', 'tests/windows/relay/']},
 'T11': {'environment': 'postgres',
         'implementation_prefixes': ['apps/api/app/modules/rounds/'],
         'owner': 'api.rounds',
         'test_prefixes': ['apps/api/tests/unit/rounds/',
                           'apps/api/tests/integration/rounds/',
                           'apps/api/tests/contracts/rounds/',
                           'apps/api/tests/replay/rounds/',
                           'tests/integration/api/rounds/',
                           'tests/windows/api/rounds/']},
 'T12': {'environment': 'postgres',
         'implementation_prefixes': ['apps/api/app/modules/rounds/'],
         'owner': 'api.rounds',
         'test_prefixes': ['apps/api/tests/unit/rounds/',
                           'apps/api/tests/integration/rounds/',
                           'apps/api/tests/contracts/rounds/',
                           'apps/api/tests/replay/rounds/',
                           'tests/integration/api/rounds/',
                           'tests/windows/api/rounds/']},
 'T13': {'environment': 'postgres',
         'implementation_prefixes': ['apps/api/app/modules/sessions/'],
         'owner': 'api.sessions',
         'test_prefixes': ['apps/api/tests/unit/sessions/',
                           'apps/api/tests/integration/sessions/',
                           'apps/api/tests/contracts/sessions/',
                           'apps/api/tests/replay/sessions/',
                           'tests/integration/api/sessions/',
                           'tests/windows/api/sessions/']},
 'T14': {'environment': 'postgres',
         'implementation_prefixes': ['apps/api/app/modules/rounds/'],
         'owner': 'api.rounds',
         'test_prefixes': ['apps/api/tests/unit/rounds/',
                           'apps/api/tests/integration/rounds/',
                           'apps/api/tests/contracts/rounds/',
                           'apps/api/tests/replay/rounds/',
                           'tests/integration/api/rounds/',
                           'tests/windows/api/rounds/']},
 'T15': {'environment': 'windows_obs',
         'implementation_prefixes': ['apps/api/app/modules/realtime/'],
         'owner': 'api.realtime',
         'test_prefixes': ['apps/api/tests/unit/realtime/',
                           'apps/api/tests/integration/realtime/',
                           'apps/api/tests/contracts/realtime/',
                           'apps/api/tests/replay/realtime/',
                           'tests/integration/api/realtime/',
                           'tests/windows/api/realtime/']},
 'T16': {'environment': 'local',
         'implementation_prefixes': ['apps/api/app/modules/auth/'],
         'owner': 'api.auth',
         'test_prefixes': ['apps/api/tests/unit/auth/',
                           'apps/api/tests/integration/auth/',
                           'apps/api/tests/contracts/auth/',
                           'apps/api/tests/replay/auth/',
                           'tests/integration/api/auth/',
                           'tests/windows/api/auth/']},
 'T17': {'environment': 'postgres',
         'implementation_prefixes': ['apps/api/app/modules/wordbank/'],
         'owner': 'api.wordbank',
         'test_prefixes': ['apps/api/tests/unit/wordbank/',
                           'apps/api/tests/integration/wordbank/',
                           'apps/api/tests/contracts/wordbank/',
                           'apps/api/tests/replay/wordbank/',
                           'tests/integration/api/wordbank/',
                           'tests/windows/api/wordbank/']},
 'T18': {'environment': 'relay_postgres',
         'implementation_prefixes': ['apps/relay/app/'],
         'owner': 'relay',
         'test_prefixes': ['apps/relay/tests/', 'tests/integration/relay/', 'tests/windows/relay/']},
 'T19': {'environment': 'postgres',
         'implementation_prefixes': ['apps/api/app/modules/rounds/'],
         'owner': 'api.rounds',
         'test_prefixes': ['apps/api/tests/unit/rounds/',
                           'apps/api/tests/integration/rounds/',
                           'apps/api/tests/contracts/rounds/',
                           'apps/api/tests/replay/rounds/',
                           'tests/integration/api/rounds/',
                           'tests/windows/api/rounds/']},
 'T20': {'environment': 'windows_obs',
         'implementation_prefixes': ['apps/api/app/modules/realtime/'],
         'owner': 'api.realtime',
         'test_prefixes': ['apps/api/tests/unit/realtime/',
                           'apps/api/tests/integration/realtime/',
                           'apps/api/tests/contracts/realtime/',
                           'apps/api/tests/replay/realtime/',
                           'tests/integration/api/realtime/',
                           'tests/windows/api/realtime/']},
 'T21': {'environment': 'model',
         'implementation_prefixes': ['apps/scorer/app/'],
         'owner': 'scorer',
         'test_prefixes': ['apps/scorer/tests/', 'tests/integration/scorer/', 'tests/windows/scorer/']},
 'T22': {'environment': 'windows_obs',
         'implementation_prefixes': ['apps/api/app/modules/operations/'],
         'owner': 'api.operations',
         'test_prefixes': ['apps/api/tests/unit/operations/',
                           'apps/api/tests/integration/operations/',
                           'apps/api/tests/contracts/operations/',
                           'apps/api/tests/replay/operations/',
                           'tests/integration/api/operations/',
                           'tests/windows/api/operations/']},
 'T23': {'environment': 'windows',
         'implementation_prefixes': ['apps/windows-launcher/', 'deploy/windows-native/'],
         'owner': 'windows-launcher',
         'test_prefixes': ['apps/windows-launcher/tests/', 'tests/windows/launcher/']},
 'T24': {'environment': 'platform',
         'implementation_prefixes': ['apps/relay/app/'],
         'owner': 'relay',
         'test_prefixes': ['apps/relay/tests/', 'tests/integration/relay/', 'tests/windows/relay/']},
 'T25': {'environment': 'platform',
         'implementation_prefixes': ['apps/relay/app/'],
         'owner': 'relay',
         'test_prefixes': ['apps/relay/tests/', 'tests/integration/relay/', 'tests/windows/relay/']},
 'T26': {'environment': 'postgres',
         'implementation_prefixes': ['apps/api/app/modules/analytics/'],
         'owner': 'api.analytics',
         'test_prefixes': ['apps/api/tests/unit/analytics/',
                           'apps/api/tests/integration/analytics/',
                           'apps/api/tests/contracts/analytics/',
                           'apps/api/tests/replay/analytics/',
                           'tests/integration/api/analytics/',
                           'tests/windows/api/analytics/']},
 'T27': {'environment': 'postgres',
         'implementation_prefixes': ['apps/api/app/modules/rounds/'],
         'owner': 'api.rounds',
         'test_prefixes': ['apps/api/tests/unit/rounds/',
                           'apps/api/tests/integration/rounds/',
                           'apps/api/tests/contracts/rounds/',
                           'apps/api/tests/replay/rounds/',
                           'tests/integration/api/rounds/',
                           'tests/windows/api/rounds/']},
 'WT01': {'environment': 'windows',
          'implementation_prefixes': ['apps/windows-launcher/', 'deploy/windows-native/'],
          'owner': 'windows-launcher',
          'test_prefixes': ['apps/windows-launcher/tests/', 'tests/windows/launcher/']},
 'WT02': {'environment': 'windows',
          'implementation_prefixes': ['apps/windows-launcher/', 'deploy/windows-native/'],
          'owner': 'windows-launcher',
          'test_prefixes': ['apps/windows-launcher/tests/', 'tests/windows/launcher/']},
 'WT03': {'environment': 'windows',
          'implementation_prefixes': ['apps/windows-launcher/', 'deploy/windows-native/'],
          'owner': 'windows-launcher',
          'test_prefixes': ['apps/windows-launcher/tests/', 'tests/windows/launcher/']},
 'WT04': {'environment': 'windows',
          'implementation_prefixes': ['apps/windows-launcher/', 'deploy/windows-native/'],
          'owner': 'windows-launcher',
          'test_prefixes': ['apps/windows-launcher/tests/', 'tests/windows/launcher/']},
 'WT05': {'environment': 'windows',
          'implementation_prefixes': ['apps/windows-launcher/', 'deploy/windows-native/'],
          'owner': 'windows-launcher',
          'test_prefixes': ['apps/windows-launcher/tests/', 'tests/windows/launcher/']},
 'WT06': {'environment': 'windows',
          'implementation_prefixes': ['apps/windows-launcher/', 'deploy/windows-native/'],
          'owner': 'windows-launcher',
          'test_prefixes': ['apps/windows-launcher/tests/', 'tests/windows/launcher/']},
 'WT07': {'environment': 'windows',
          'implementation_prefixes': ['apps/windows-launcher/', 'deploy/windows-native/'],
          'owner': 'windows-launcher',
          'test_prefixes': ['apps/windows-launcher/tests/', 'tests/windows/launcher/']},
 'WT08': {'environment': 'windows',
          'implementation_prefixes': ['apps/windows-launcher/', 'deploy/windows-native/'],
          'owner': 'windows-launcher',
          'test_prefixes': ['apps/windows-launcher/tests/', 'tests/windows/launcher/']},
 'WT09': {'environment': 'windows',
          'implementation_prefixes': ['apps/windows-launcher/', 'deploy/windows-native/'],
          'owner': 'windows-launcher',
          'test_prefixes': ['apps/windows-launcher/tests/', 'tests/windows/launcher/']},
 'WT10': {'environment': 'windows',
          'implementation_prefixes': ['apps/windows-launcher/', 'deploy/windows-native/'],
          'owner': 'windows-launcher',
          'test_prefixes': ['apps/windows-launcher/tests/', 'tests/windows/launcher/']},
 'WT11': {'environment': 'windows',
          'implementation_prefixes': ['apps/windows-launcher/', 'deploy/windows-native/'],
          'owner': 'windows-launcher',
          'test_prefixes': ['apps/windows-launcher/tests/', 'tests/windows/launcher/']},
 'WT12': {'environment': 'windows',
          'implementation_prefixes': ['apps/windows-launcher/', 'deploy/windows-native/'],
          'owner': 'windows-launcher',
          'test_prefixes': ['apps/windows-launcher/tests/', 'tests/windows/launcher/']},
 'WT13': {'environment': 'windows',
          'implementation_prefixes': ['apps/relay/app/'],
          'owner': 'relay',
          'test_prefixes': ['apps/relay/tests/', 'tests/integration/relay/', 'tests/windows/relay/']},
 'WT14': {'environment': 'windows',
          'implementation_prefixes': ['apps/relay/app/'],
          'owner': 'relay',
          'test_prefixes': ['apps/relay/tests/', 'tests/integration/relay/', 'tests/windows/relay/']},
 'WT15': {'environment': 'windows',
          'implementation_prefixes': ['apps/relay/app/'],
          'owner': 'relay',
          'test_prefixes': ['apps/relay/tests/', 'tests/integration/relay/', 'tests/windows/relay/']},
 'WT16': {'environment': 'windows',
          'implementation_prefixes': ['apps/relay/app/'],
          'owner': 'relay',
          'test_prefixes': ['apps/relay/tests/', 'tests/integration/relay/', 'tests/windows/relay/']},
 'WT17': {'environment': 'windows',
          'implementation_prefixes': ['apps/relay/app/'],
          'owner': 'relay',
          'test_prefixes': ['apps/relay/tests/', 'tests/integration/relay/', 'tests/windows/relay/']},
 'WT18': {'environment': 'windows',
          'implementation_prefixes': ['apps/relay/app/'],
          'owner': 'relay',
          'test_prefixes': ['apps/relay/tests/', 'tests/integration/relay/', 'tests/windows/relay/']},
 'WT19': {'environment': 'windows',
          'implementation_prefixes': ['apps/relay/app/'],
          'owner': 'relay',
          'test_prefixes': ['apps/relay/tests/', 'tests/integration/relay/', 'tests/windows/relay/']},
 'WT20': {'environment': 'windows',
          'implementation_prefixes': ['apps/relay/app/'],
          'owner': 'relay',
          'test_prefixes': ['apps/relay/tests/', 'tests/integration/relay/', 'tests/windows/relay/']},
 'WT21': {'environment': 'windows',
          'implementation_prefixes': ['apps/relay/app/'],
          'owner': 'relay',
          'test_prefixes': ['apps/relay/tests/', 'tests/integration/relay/', 'tests/windows/relay/']},
 'WT22': {'environment': 'windows',
          'implementation_prefixes': ['apps/windows-launcher/', 'deploy/windows-native/'],
          'owner': 'windows-launcher',
          'test_prefixes': ['apps/windows-launcher/tests/', 'tests/windows/launcher/']},
 'WT23': {'environment': 'windows',
          'implementation_prefixes': ['apps/windows-launcher/', 'deploy/windows-native/'],
          'owner': 'windows-launcher',
          'test_prefixes': ['apps/windows-launcher/tests/', 'tests/windows/launcher/']},
 'WT24': {'environment': 'platform',
          'implementation_prefixes': ['apps/relay/app/'],
          'owner': 'relay',
          'test_prefixes': ['apps/relay/tests/', 'tests/integration/relay/', 'tests/windows/relay/']}}

REQUIRED_IDS = set(BOUNDARIES)


def safe_file(root, value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise ValueError('invalid repository-relative path')
    path = Path(value)
    if path.is_absolute() or any(part in ('', '.', '..') for part in value.split('/')):
        raise ValueError('evidence paths must be repository-relative')
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError('evidence escapes repository')
    return resolved


def under(path, prefixes):
    return isinstance(path, str) and any(path.startswith(p) if p.endswith('/') else path == p for p in prefixes)


def test_source_matches(root, verification):
    """Static source verification plus runner collection evidence; never import arbitrary tests."""
    node = verification['node_id']
    parts = node.split('::')
    if len(parts) not in (2, 3) or not parts[0].endswith('.py'):
        return False
    test_path = safe_file(root, parts[0])
    if not test_path.name.startswith('test_'):
        return False
    try:
        module = ast.parse(test_path.read_text())
    except (OSError, SyntaxError, UnicodeError):
        return False
    parents = module.body
    if len(parts) == 3:
        classes = [n for n in module.body if isinstance(n, ast.ClassDef) and n.name == parts[1]]
        if len(classes) != 1:
            return False
        parents = classes[0].body
    name = parts[-1]
    functions = [n for n in parents if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    # A declared executable test is required, never a missing or empty test source.
    if not name.startswith('test_') or len(functions) != 1:
        return False
    body = functions[0].body
    if not body or all(isinstance(n, (ast.Pass, ast.Expr)) and
                       (isinstance(n, ast.Pass) or isinstance(n.value, ast.Constant)) for n in body):
        return False
    expected_class = parts[0][:-3].replace('/', '.') + ('.' + parts[1] if len(parts) == 3 else '')
    return verification.get('junit_classname') == expected_class and verification.get('junit_name') == name


def validate_manifest(manifest, root=None):
    try:
        checks = manifest['checks']
        ids = [c['id'] for c in checks]
        if (manifest.get('manifest_version') != 1 or manifest.get('boundary_version') != BOUNDARY_VERSION
                or manifest.get('spec_sha256') != SPEC_SHA256 or len(ids) != len(set(ids))
                or set(ids) != REQUIRED_IDS):
            return False
        bindings = {}
        for item in checks:
            fixed = BOUNDARIES[item['id']]
            if (item.get('spec_sha256') != SPEC_SHA256 or item.get('owner') != fixed['owner']
                    or item.get('required_environment') != fixed['environment']
                    or not isinstance(item.get('requirement'), str) or not item['requirement']
                    or not isinstance(item.get('work_packages'), list)):
                return False
            paths, verification = item['implementation_paths'], item['verification']
            if not isinstance(paths, list) or len(paths) != len(set(paths)) or not isinstance(verification, list):
                return False
            allowed = fixed['implementation_prefixes'] + fixed['test_prefixes']
            if any(not under(path, allowed) for path in paths):
                return False
            if verification and (not paths or not any(under(path, fixed['implementation_prefixes']) for path in paths)
                    or not set(fixed.get('required_implementation_paths', [])) <= set(paths)):
                return False
            nodes = []
            for expected in verification:
                node = expected['node_id']
                test_path = node.split('::')[0]
                if (expected.get('environment') != fixed['environment'] or not under(test_path, fixed['test_prefixes'])
                        or test_path not in paths or not isinstance(expected.get('command'), list)):
                    return False
                if root is not None and (not test_source_matches(root, expected)
                        or any(not safe_file(root, path).is_file() for path in paths)):
                    return False
                nodes.append(node)
                bindings.setdefault(node, set()).add(fixed['owner'])
            if len(nodes) != len(set(nodes)):
                return False
        # One generic test must not be rebound across unrelated component owners.
        return all(len(owners) == 1 for owners in bindings.values())
    except (KeyError, TypeError, ValueError, OSError):
        return False


def junit_passed(raw, classname, name):
    try:
        document = ET.fromstring(raw)
    except ET.ParseError:
        return False
    cases = [c for c in document.iter('testcase') if c.get('classname') == classname and c.get('name') == name]
    return len(cases) == 1 and not any(child.tag in ('skipped', 'failure', 'error') for child in cases[0])


def report_time(value):
    instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if instant.tzinfo is None:
        raise ValueError('timezone is required')
    return instant.astimezone(timezone.utc)


def all_contracts_defined(root):
    # Only the fixed sibling checker may execute; no manifest-provided module or command is run.
    checker_path = Path(__file__).resolve().parents[2]/'packages/contracts/check_contracts.py'
    try:
        spec = importlib.util.spec_from_file_location('completion_contract_checker', checker_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        report = module.check(root)
        return report.get('valid') is True and report.get('all_contracts_defined') is True
    except (OSError, ValueError, AttributeError, ImportError):
        return False


def evaluate(root, reports, local_only=False):
    root = Path(root)
    manifest = json.loads((root/'docs/acceptance/backend-checks.json').read_text())
    source = root/SPEC_PATH
    source_matches = source.is_file() and hashlib.sha256(source.read_bytes()).hexdigest() == SPEC_SHA256
    manifest_valid = validate_manifest(manifest, root)
    entries = []
    for report in reports:
        if (report.get('report_version') != 1 or report.get('spec_sha256') != SPEC_SHA256
                or not report.get('run_id') or not report.get('started_at') or not report.get('finished_at')):
            continue
        try:
            start, finish = report_time(report['started_at']), report_time(report['finished_at'])
            if finish < start:
                continue
        except (ValueError, TypeError, AttributeError):
            continue
        collected = report.get('collected_node_ids', [])
        entries.extend(dict(e, _finished=finish, _collected=collected) for e in report.get('checks', []))
    results = []
    for item in manifest['checks']:
        expected = item.get('verification', [])
        outcomes = []
        for verification in expected:
            evidence = [e for e in entries if e.get('id') == item['id'] and e.get('node_id') == verification['node_id']]
            verified = False
            # Compare actual UTC instants. Equal-time contradictions fail closed.
            latest = max((e['_finished'] for e in evidence), default=None)
            evidence = [e for e in evidence if e['_finished'] == latest]
            for entry in evidence:
                verified = False
                if (entry.get('outcome') != 'passed' or entry.get('executed') is not True
                        or entry.get('exit_code') != 0 or entry.get('tests_run', 0) < 1
                        or entry.get('skipped', 1) != 0 or entry.get('environment') != verification['environment']
                        or verification['node_id'] not in entry['_collected']):
                    verified = False
                    break
                try:
                    artifact = safe_file(root, entry['artifact_path'])
                    raw_artifact = artifact.read_bytes()
                    if hashlib.sha256(raw_artifact).hexdigest() != entry['artifact_sha256']:
                        break
                    if entry.get('artifact_format') != 'junit' or not junit_passed(raw_artifact, verification['junit_classname'], verification['junit_name']):
                        break
                    files = entry.get('implementation_sha256', {})
                    if not item['implementation_paths'] or set(files) != set(item['implementation_paths']):
                        break
                    if any(hashlib.sha256(safe_file(root, path).read_bytes()).hexdigest() != files[path] for path in files):
                        break
                except (ValueError, OSError, KeyError, TypeError):
                    break
                verified = True
            outcomes.append(verified)
        passed = source_matches and manifest_valid and bool(outcomes) and all(outcomes)
        state = 'passed' if passed else ('pending_external' if item['required_environment'] != 'local' else 'not_implemented')
        results.append({'id': item['id'], 'status': state, 'required_environment':item['required_environment']})
    local = [r for r in results if r['required_environment'] == 'local']
    contracts_complete = all_contracts_defined(root)
    all_complete = manifest_valid and contracts_complete and bool(results) and all(r['status'] == 'passed' for r in results)
    local_complete = manifest_valid and bool(local) and all(r['status'] == 'passed' for r in local)
    return {'report_version':1,'spec_sha256':SPEC_SHA256,'source_matches':source_matches,'manifest_valid':manifest_valid,
            'boundary_version':BOUNDARY_VERSION, 'contracts_complete':contracts_complete,
            'mode':'local_only' if local_only else 'overall','all_complete':all_complete,
            'local_complete':local_complete,'passed':sum(r['status']=='passed' for r in results),
            'total':len(results),'checks':results}


TEST_ROOTS = ('packages/contracts/tests/', 'scripts/backend/tests/', 'apps/api/tests/',
              'apps/scorer/tests/', 'apps/relay/tests/', 'apps/windows-launcher/tests/',
              'tests/unit/', 'tests/integration/', 'tests/windows/', 'tests/load/', 'tests/faults/')



def build_progress(root, reports):
    """Aggregate verified evidence separately from strict whole-Spec checks."""
    root = Path(root)
    try:
        source_matches = hashlib.sha256(safe_file(root,SPEC_PATH).read_bytes()).hexdigest() == SPEC_SHA256
    except (OSError,ValueError):
        source_matches = False
    manifest_hash = hashlib.sha256(safe_file(root,'docs/acceptance/backend-checks.json').read_bytes()).hexdigest()
    latest = {}
    for report in reports:
        if report.get('spec_sha256') != SPEC_SHA256:
            continue
        try:
            finished = report_time(report['finished_at'])
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        for original in report.get('component_results', []):
            component = dict(original)
            valid = source_matches and report.get('manifest_sha256') == manifest_hash
            try:
                artifact = safe_file(root,component['artifact_path']).read_bytes()
                valid = valid and hashlib.sha256(artifact).hexdigest()==component['artifact_sha256']
                cases = list(ET.fromstring(artifact).iter('testcase'))
                if 'junit_cases' in component:
                    selected={tuple(pair) for pair in component['junit_cases']}
                    cases=[case for case in cases if (case.get('classname'),case.get('name')) in selected]
                valid = valid and bool(cases) and len(cases)==component['tests_run']
                valid = valid and not any(child.tag in ('skipped','failure','error') for case in cases for child in case)
                hashes=component['source_sha256']
                valid = valid and bool(hashes) and all(hashlib.sha256(safe_file(root,path).read_bytes()).hexdigest()==digest for path,digest in hashes.items())
                valid = valid and bool(component['collected_node_ids']) and set(component['collected_node_ids']) <= set(report['collected_node_ids'])
            except (OSError,ValueError,KeyError,TypeError,ET.ParseError):
                valid=False
            runtime=report.get('observed_runtime',{})
            if component.get('stage')=='external' or (component.get('stage')=='integration' and runtime.get('integration_qualified') is not True):
                valid=False
                component['status']='unverified'
            component['source_matches']=source_matches
            if not source_matches:
                component['status']='stale' if original.get('status')=='passed' else 'unverified'
            elif not valid:
                component['status']='stale' if component.get('status')=='passed' else 'not_passed'
            component['stages']={stage:('source_bound' if stage=='code' and valid else
                                      'passed' if stage==component['stage'] and component['status']=='passed' else 'pending')
                                 for stage in ('code','test','integration','external')}
            key=component['component_id']
            if key not in latest or finished>=latest[key][0]:
                latest[key]=(finished,component)
    components=[value[1] for value in latest.values()]
    return {'progress_version':1,'spec_sha256':SPEC_SHA256,'source_matches':source_matches,'generated_at':datetime.now(timezone.utc).isoformat(),
            'full_spec':evaluate(root,reports),'component_results':components,
            'stages':{stage:{'passed_components':sum(c['stage']==stage and c['status']=='passed' for c in components),
                             'whole_stage_complete':False} for stage in ('test','integration','external')},
            'note':'Component evidence does not discharge an entire BE acceptance group or release gate.'}



def observed_test_runtime(root, collected, environment, service, cwd, env):
    """Record host facts and require matching real-integration nodes, not a CLI label."""
    postgres_nodes = [node for node in collected if node.startswith('apps/api/tests/integration/')]
    relay_nodes = [node for node in collected if node.startswith('apps/relay/tests/integration/')]
    model_nodes = []
    for node in collected:
        if node.startswith('apps/scorer/tests/integration/'):
            source = safe_file(root,node.split('::')[0]).read_text()
            if '@pytest.mark.real_model' in source:
                model_nodes.append(node)
    postgres = bool(postgres_nodes and env.get('WORDHEAT_TEST_CONFIG'))
    relay_postgres = bool(relay_nodes and env.get('WORDHEAT_TEST_CONFIG'))
    model = bool(model_nodes and env.get('WORDHEAT_TEST_MODEL_MANIFEST') and env.get('WORDHEAT_TEST_MODEL_ROOT'))
    qualified = {'local':True,'postgres':postgres,'relay_postgres':relay_postgres,
                 'model':model,'postgres_model':postgres and model}.get(environment,False)
    return {'sys_platform':sys.platform,'python_version':sys.version.split()[0],
            'service':service,'cwd':str(cwd.relative_to(root)) if cwd!=root else '.',
            'postgres_nodes':postgres_nodes,'relay_postgres_nodes':relay_nodes,'real_model_nodes':model_nodes,
            'integration_qualified':qualified,'external_runner_available':False}


def run_pytest_evidence(root, test_paths, environment, evidence_dir):
    """Run fixed pytest argv against project tests. No shell, manifest commands, or arbitrary options."""
    root = Path(root).resolve()
    if environment not in {x['environment'] for x in BOUNDARIES.values()}:
        raise ValueError('unknown fixed environment')
    if environment == 'windows' and sys.platform != 'win32':
        raise ValueError('Windows evidence requires Windows; observed sys.platform=' + sys.platform)
    if environment in ('windows','windows_obs','platform'):
        raise ValueError('dedicated external validation runner is unavailable; declared environment cannot prove external acceptance')
    if not test_paths:
        raise ValueError('at least one test path is required')
    services={value.split('/')[1] for value in test_paths if isinstance(value,str) and value.startswith('apps/')}
    if len(services)>1:
        raise ValueError('cross-service pytest batch is prohibited; run each service separately and aggregate reports')
    service=next(iter(services),None)
    cwd=safe_file(root,'apps/'+service) if service else root
    for value in test_paths:
        path = safe_file(root, value)
        if not under(value.rstrip('/')+'/' if path.is_dir() else value, TEST_ROOTS) or not path.exists():
            raise ValueError('runner accepts existing project test paths only')
        if path.is_file() and (not path.name.startswith('test_') or path.suffix != '.py'):
            raise ValueError('runner accepts Python test files only')
    source = safe_file(root, SPEC_PATH)
    if hashlib.sha256(source.read_bytes()).hexdigest() != SPEC_SHA256:
        raise ValueError('runner requires exact reviewed backend Spec')
    manifest_path = safe_file(root, 'docs/acceptance/backend-checks.json')
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    if not under(evidence_dir.rstrip('/')+'/', ['docs/acceptance/']):
        raise ValueError('evidence output must be under docs/acceptance')
    run_id = uuid.uuid4().hex
    out = safe_file(root, evidence_dir)/run_id
    out.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    # A fresh process sees exactly one app package; never inherit another service's PYTHONPATH.
    env['PYTHONPATH']=str(cwd) if service else str(root)
    pytest_paths=[str(safe_file(root,value)) for value in test_paths]
    fixed = [sys.executable, '-B', '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '--rootdir='+str(root)]
    collection = subprocess.run(fixed+['--collect-only']+pytest_paths, cwd=cwd, env=env,
                                capture_output=True, text=True, timeout=120, shell=False)
    (out/'collection.txt').write_text(collection.stdout+collection.stderr)
    collected = [line.strip() for line in collection.stdout.splitlines()
                 if '::' in line and any(line.strip().startswith(value.rstrip('/') ) for value in test_paths)]
    if collection.returncode or not collected:
        raise ValueError('pytest collection failed or collected no tests; see collection.txt')
    before_paths={node.split('::')[0] for node in collected}
    owned_roots=set()
    for selected in test_paths:
        if selected.startswith('apps/api/tests'):
            owned_roots.update(('apps/api/app','apps/api/migrations','packages/contracts'))
        elif selected.startswith('apps/scorer/tests'):
            owned_roots.update(('apps/scorer/app','apps/api/app/modules/scoring'))
        elif selected.startswith('packages/contracts/tests'):
            owned_roots.add('packages/contracts')
        elif selected.startswith('scripts/backend/tests'):
            owned_roots.add('scripts/backend')
        elif selected.startswith('apps/relay/tests'):
            owned_roots.add('apps/relay/app')
        elif selected.startswith('apps/windows-launcher/tests'):
            owned_roots.add('apps/windows-launcher')
    for owned in owned_roots:
        directory=safe_file(root,owned)
        if directory.is_dir():
            before_paths.update(str(path.relative_to(root)) for path in directory.rglob('*')
                                if path.is_file() and path.suffix in ('.py','.sql','.json','.yaml') and '__pycache__' not in path.parts)
    for item in manifest.get('checks',[]):
        if any(v['node_id'] in collected for v in item.get('verification',[])):
            before_paths.update(item['implementation_paths'])
    before_hashes={path:hashlib.sha256(safe_file(root,path).read_bytes()).hexdigest() for path in before_paths}
    junit = out/'results.xml'
    run = subprocess.run(fixed+['--junitxml='+str(junit)]+pytest_paths, cwd=cwd, env=env,
                         capture_output=True, text=True, timeout=120, shell=False)
    (out/'output.txt').write_text(run.stdout+run.stderr)
    if manifest_path.read_bytes()!=manifest_raw or any(hashlib.sha256(safe_file(root,path).read_bytes()).hexdigest()!=digest for path,digest in before_hashes.items()):
        raise ValueError('test sources or manifest changed during execution; evidence rejected')
    finished = datetime.now(timezone.utc).isoformat()
    raw = junit.read_bytes()
    xml = ET.fromstring(raw)
    cases = list(xml.iter('testcase'))
    artifact = str(junit.relative_to(root))
    digest = hashlib.sha256(raw).hexdigest()
    source_paths = set(before_paths)
    observed_runtime=observed_test_runtime(root,collected,environment,service,cwd,env)
    environment_observed=observed_runtime['integration_qualified']
    checks = []
    for item in manifest.get('checks', []):
        for verification in item.get('verification', []):
            node = verification['node_id']
            if node not in collected or verification['environment'] != environment:
                continue
            paths = item['implementation_paths']
            if not paths or any(not safe_file(root, path).is_file() for path in paths):
                continue
            source_paths.update(paths)
            matched = [case for case in cases if case.get('classname') == verification['junit_classname']
                       and case.get('name') == verification['junit_name']]
            skipped = sum(any(child.tag == 'skipped' for child in case) for case in matched)
            passed = environment_observed and len(matched) == 1 and junit_passed(raw, verification['junit_classname'], verification['junit_name'])
            checks.append({'id':item['id'],'node_id':node,'outcome':'passed' if passed else 'failed',
                           'executed':True,'exit_code':run.returncode,'tests_run':len(matched),'skipped':skipped,
                           'environment':environment,'artifact_format':'junit','artifact_path':artifact,
                           'artifact_sha256':digest,'implementation_sha256':{path:hashlib.sha256(safe_file(root,path).read_bytes()).hexdigest() for path in paths}})
    source_hashes = {path:hashlib.sha256(safe_file(root,path).read_bytes()).hexdigest() for path in sorted(source_paths)}
    skipped_total = sum(any(child.tag == 'skipped' for child in case) for case in cases)
    failed_total = sum(any(child.tag in ('failure','error') for child in case) for case in cases)
    stage = 'test' if environment == 'local' else ('integration' if environment in ('postgres','relay_postgres','model','postgres_model') else 'external')
    components = [{'component_id':'+'.join(test_paths),'stage':stage,
                   'status':('unverified' if not environment_observed else 'passed' if run.returncode == 0 and cases and skipped_total == 0 and failed_total == 0 else 'not_passed'),
                   'declared_environment':environment,'observed_runtime':observed_runtime,
                   'environment':environment,'tests_run':len(cases),'skipped':skipped_total,'failed':failed_total,
                   'independently_verified':False,'artifact_path':artifact,'artifact_sha256':digest,
                   'source_sha256':source_hashes,'collected_node_ids':collected}]
    subcase_results=[]
    for mapping in manifest.get('component_subcases',[]):
        node=mapping['node_id']
        if node not in collected or mapping['environment']!=environment:
            continue
        test_path=node.split('::')[0]
        if source_hashes.get(test_path)!=mapping['test_source_sha256']:
            continue
        parts=node.split('::')
        classname=parts[0][:-3].replace('/','.')+('.'+parts[1] if len(parts)==3 else '')
        name=parts[-1]
        matched=[case for case in cases if case.get('classname')==classname and case.get('name')==name]
        outcome='passed' if environment_observed and run.returncode==0 and junit_passed(raw,classname,name) else 'not_passed'
        subcase_results.append({'subcase_id':mapping['subcase_id'],'node_id':node,'status':outcome,
                               'group_refs':mapping['group_refs'],'entire_acceptance_group_complete':False})
        components.append(dict(components[0],component_id=mapping['subcase_id'],stage=mapping['stage'],
                               status=outcome,tests_run=len(matched),
                               skipped=sum(any(child.tag=='skipped' for child in case) for case in matched),
                               failed=sum(any(child.tag in ('failure','error') for child in case) for case in matched),
                               collected_node_ids=[node],junit_cases=[[classname,name]],
                               group_refs=mapping['group_refs'],entire_acceptance_group_complete=False))
    report = {'report_version':1,'spec_sha256':SPEC_SHA256,'manifest_sha256':hashlib.sha256(manifest_raw).hexdigest(),
              'run_id':run_id,'started_at':started,'finished_at':finished,'collected_node_ids':collected,
              'declared_environment':environment,'observed_runtime':observed_runtime,
              'exit_code':run.returncode,'junit_path':artifact,'checks':checks,'component_results':components,
              'subcase_results':subcase_results}
    (out/'evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    progress_path=safe_file(root,'docs/acceptance/backend-progress.json')
    prior_reports=[]
    evidence_paths=[]
    if progress_path.is_file():
        try:
            for path in json.loads(progress_path.read_text()).get('evidence_paths',[]):
                prior_reports.append(json.loads(safe_file(root,path).read_text()))
                evidence_paths.append(path)
        except (OSError,ValueError,TypeError):
            prior_reports=[]
            evidence_paths=[]
    evidence_paths.append(str((out/'evidence.json').relative_to(root)))
    progress=build_progress(root,prior_reports+[report])
    progress['evidence_paths']=evidence_paths
    progress_path.write_text(json.dumps(progress,ensure_ascii=False,indent=2)+'\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--report', type=Path, action='append', default=[])
    parser.add_argument('--write-progress', action='store_true', help='Aggregate supplied reports to fixed backend-progress.json')
    parser.add_argument('--local-only', action='store_true')
    parser.add_argument('--run-tests', action='append', default=[], help='Existing project test file/directory; repeat to select multiple')
    parser.add_argument('--environment', default='local', choices=sorted({x['environment'] for x in BOUNDARIES.values()}))
    parser.add_argument('--evidence-dir', default='docs/acceptance/backend-evidence')
    args = parser.parse_args()
    try:
        if args.run_tests:
            result = run_pytest_evidence(args.root,args.run_tests,args.environment,args.evidence_dir)
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 0 if result['exit_code']==0 else 1
        reports=[json.loads(p.read_text()) for p in args.report]
        if args.write_progress:
            result=build_progress(args.root,reports)
            result['evidence_paths']=[str(p.resolve().relative_to(args.root.resolve())) for p in args.report]
            safe_file(args.root,'docs/acceptance/backend-progress.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 0
        result = evaluate(args.root, reports, args.local_only)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'report_version':1,'all_complete':False,'error':str(exc),
                          'observed_runtime':{'sys_platform':sys.platform,'python_version':sys.version.split()[0]}}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if (result['local_complete'] if args.local_only else result['all_complete']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
