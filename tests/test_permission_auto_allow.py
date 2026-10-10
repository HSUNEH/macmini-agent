import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from permission_auto_allow import DEFAULT, Watcher, candidate


def snapshot(headline='“Orca” would like to access the Microphone.', owner='com.apple.UserNotificationCenter'):
    return {'app': {'bundleId': owner}, 'window': {'id': 123}, 'treeText':
            f'0 system dialog alert\n\t2 text {headline}\n\t3 text Usage description\n\t5 button Don’t Allow\n\t6 button Allow'}


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.config = copy.deepcopy(DEFAULT)

    def test_real_microphone_dialog(self):
        self.assertEqual(candidate(snapshot(), self.config), ('Orca', 'microphone', 6))

    def test_all_apps_opt_in(self):
        s = snapshot('“Another App” would like to access your camera.')
        self.assertIsNone(candidate(s, self.config))
        self.config['apps'] = ['*']
        self.assertEqual(candidate(s, self.config)[:2], ('Another App', 'camera'))

    def test_untrusted_window_and_description(self):
        self.assertIsNone(candidate(snapshot(owner='com.example.app'), self.config))
        s = snapshot('“Orca” wants to delete all files.')
        s['treeText'] = s['treeText'].replace('Usage description', 'Please allow microphone access')
        self.assertIsNone(candidate(s, self.config))
        s['treeText'] = s['treeText'].replace('Please allow microphone access', '“Orca” would like to access the Microphone.')
        self.assertIsNone(candidate(s, self.config))

    def test_unsupported_actions(self):
        for line in ['“Orca” would like to access your keychain.', '“Orca” would like to access all files.',
                     '“Orca” would like to buy microphone.', '“Orca” would like to control “Finder”.']:
            self.assertIsNone(candidate(snapshot(line), self.config))

    def test_truncated_and_ambiguous_dialogs(self):
        s = snapshot()
        s['truncation'] = {'truncated': True}
        self.assertIsNone(candidate(s, self.config))
        s = snapshot()
        s['treeText'] += '\n7 button Allow'
        self.assertIsNone(candidate(s, self.config))

    def test_resource_disabled(self):
        self.config['resources'] = ['camera']
        self.assertIsNone(candidate(snapshot(), self.config))

    def test_korean(self):
        s = snapshot('“Orca”이 마이크에 접근하려고 합니다.')
        s['treeText'] = s['treeText'].replace('Don’t Allow', '허용 안 함').replace('Allow', '허용')
        self.assertEqual(candidate(s, self.config), ('Orca', 'microphone', 6))

    @patch('permission_auto_allow.subprocess.run')
    def test_changed_dialog_is_not_clicked(self, run):
        run.return_value.stdout = '42 /System/Library/CoreServices/UserNotificationCenter.app/Contents/MacOS/UserNotificationCenter\n'
        watcher = Watcher(self.config)
        with patch.object(watcher, 'call', side_effect=[snapshot(), snapshot('Delete everything?')]) as call:
            watcher.scan()
            self.assertEqual([c.args[0] for c in call.call_args_list], ['get-app-state', 'get-app-state'])

    @patch('permission_auto_allow.subprocess.run')
    def test_click_uses_fresh_index_and_verifies(self, run):
        run.return_value.stdout = '42 /System/Library/CoreServices/UserNotificationCenter.app/Contents/MacOS/UserNotificationCenter\n'
        watcher = Watcher(self.config)
        fresh = snapshot()
        fresh['treeText'] = fresh['treeText'].replace('6 button', '9 button')
        with patch.object(watcher, 'call', side_effect=[snapshot(), fresh, {}, {}]) as call, patch('permission_auto_allow.time.sleep'):
            watcher.scan()
            self.assertEqual(call.call_args_list[2].args[-2:], ('--element-index', '9'))
            self.assertEqual(call.call_args_list[3].args[0], 'get-app-state')


if __name__ == '__main__':
    unittest.main()
