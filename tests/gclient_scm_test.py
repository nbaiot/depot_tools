#!/usr/bin/env vpython3
# Copyright (c) 2012 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Unit tests for gclient_scm.py."""

# pylint: disable=E1103

from shutil import rmtree
from subprocess import Popen, PIPE, STDOUT

import json
import logging
import os
import re
import sys
import tempfile
import unittest

if sys.version_info.major == 2:
  from cStringIO import StringIO
else:
  from io import StringIO

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from third_party import mock
from testing_support import fake_repos
from testing_support import test_case_utils

import gclient_scm
import git_cache
import subprocess2

# Disable global git cache
git_cache.Mirror.SetCachePath(None)

# Shortcut since this function is used often
join = gclient_scm.os.path.join

TIMESTAMP_RE = re.compile('\[[0-9]{1,2}:[0-9]{2}:[0-9]{2}\] (.*)', re.DOTALL)
def strip_timestamps(value):
  lines = value.splitlines(True)
  for i in range(len(lines)):
    m = TIMESTAMP_RE.match(lines[i])
    if m:
      lines[i] = m.group(1)
  return ''.join(lines)


class BasicTests(unittest.TestCase):
  @mock.patch('gclient_scm.scm.GIT.Capture')
  def testGetFirstRemoteUrl(self, mockCapture):
    REMOTE_STRINGS = [('remote.origin.url E:\\foo\\bar', 'E:\\foo\\bar'),
                      ('remote.origin.url /b/foo/bar', '/b/foo/bar'),
                      ('remote.origin.url https://foo/bar', 'https://foo/bar'),
                      ('remote.origin.url E:\\Fo Bar\\bax', 'E:\\Fo Bar\\bax'),
                      ('remote.origin.url git://what/"do', 'git://what/"do')]
    FAKE_PATH = '/fake/path'
    mockCapture.side_effect = [question for question, _ in REMOTE_STRINGS]

    for _, answer in REMOTE_STRINGS:
      self.assertEqual(
          gclient_scm.SCMWrapper._get_first_remote_url(FAKE_PATH), answer)

    expected_calls = [
        mock.call(['config', '--local', '--get-regexp', r'remote.*.url'],
                   cwd=FAKE_PATH)
        for _ in REMOTE_STRINGS
    ]
    self.assertEqual(mockCapture.mock_calls, expected_calls)


class BaseGitWrapperTestCase(unittest.TestCase, test_case_utils.TestCaseUtils):
  """This class doesn't use pymox."""
  class OptionsObject(object):
    def __init__(self, verbose=False, revision=None):
      self.auto_rebase = False
      self.verbose = verbose
      self.revision = revision
      self.deps_os = None
      self.force = False
      self.reset = False
      self.nohooks = False
      self.no_history = False
      self.upstream = False
      self.cache_dir = None
      self.merge = False
      self.jobs = 1
      self.break_repo_locks = False
      self.delete_unversioned_trees = False
      self.patch_ref = None
      self.patch_repo = None
      self.rebase_patch_ref = True
      self.reset_patch_ref = True

  sample_git_import = """blob
mark :1
data 6
Hello

blob
mark :2
data 4
Bye

reset refs/heads/master
commit refs/heads/master
mark :3
author Bob <bob@example.com> 1253744361 -0700
committer Bob <bob@example.com> 1253744361 -0700
data 8
A and B
M 100644 :1 a
M 100644 :2 b

blob
mark :4
data 10
Hello
You

blob
mark :5
data 8
Bye
You

commit refs/heads/origin
mark :6
author Alice <alice@example.com> 1253744424 -0700
committer Alice <alice@example.com> 1253744424 -0700
data 13
Personalized
from :3
M 100644 :4 a
M 100644 :5 b

blob
mark :7
data 5
Mooh

commit refs/heads/feature
mark :8
author Bob <bob@example.com> 1390311986 -0000
committer Bob <bob@example.com> 1390311986 -0000
data 6
Add C
from :3
M 100644 :7 c

reset refs/heads/master
from :3
"""
  def Options(self, *args, **kwargs):
    return self.OptionsObject(*args, **kwargs)

  def checkstdout(self, expected):
    value = sys.stdout.getvalue()
    sys.stdout.close()
    # pylint: disable=no-member
    self.assertEqual(expected, strip_timestamps(value))

  @staticmethod
  def CreateGitRepo(git_import, path):
    """Do it for real."""
    try:
      Popen(['git', 'init', '-q'], stdout=PIPE, stderr=STDOUT,
            cwd=path).communicate()
    except OSError:
      # git is not available, skip this test.
      return False
    Popen(['git', 'fast-import', '--quiet'], stdin=PIPE, stdout=PIPE,
        stderr=STDOUT, cwd=path).communicate(input=git_import.encode())
    Popen(['git', 'checkout', '-q'], stdout=PIPE, stderr=STDOUT,
        cwd=path).communicate()
    Popen(['git', 'remote', 'add', '-f', 'origin', '.'], stdout=PIPE,
        stderr=STDOUT, cwd=path).communicate()
    Popen(['git', 'checkout', '-b', 'new', 'origin/master', '-q'], stdout=PIPE,
        stderr=STDOUT, cwd=path).communicate()
    Popen(['git', 'push', 'origin', 'origin/origin:origin/master', '-q'],
        stdout=PIPE, stderr=STDOUT, cwd=path).communicate()
    Popen(['git', 'config', '--unset', 'remote.origin.fetch'], stdout=PIPE,
        stderr=STDOUT, cwd=path).communicate()
    Popen(['git', 'config', 'user.email', 'someuser@chromium.org'], stdout=PIPE,
        stderr=STDOUT, cwd=path).communicate()
    Popen(['git', 'config', 'user.name', 'Some User'], stdout=PIPE,
        stderr=STDOUT, cwd=path).communicate()
    return True

  def _GetAskForDataCallback(self, expected_prompt, return_value):
    def AskForData(prompt, options):
      self.assertEqual(prompt, expected_prompt)
      return return_value
    return AskForData

  def setUp(self):
    unittest.TestCase.setUp(self)
    test_case_utils.TestCaseUtils.setUp(self)
    self.url = 'git://foo'
    # The .git suffix allows gclient_scm to recognize the dir as a git repo
    # when cloning it locally
    self.root_dir = tempfile.mkdtemp('.git')
    self.relpath = '.'
    self.base_path = join(self.root_dir, self.relpath)
    self.enabled = self.CreateGitRepo(self.sample_git_import, self.base_path)
    self._original_GitBinaryExists = gclient_scm.GitWrapper.BinaryExists
    mock.patch('gclient_scm.GitWrapper.BinaryExists',
               staticmethod(lambda : True)).start()
    mock.patch('sys.stdout', StringIO()).start()
    self.addCleanup(mock.patch.stopall)
    self.addCleanup(lambda: rmtree(self.root_dir))


class ManagedGitWrapperTestCase(BaseGitWrapperTestCase):

  def testRevertMissing(self):
    if not self.enabled:
      return
    options = self.Options()
    file_path = join(self.base_path, 'a')
    scm = gclient_scm.GitWrapper(self.url, self.root_dir,
                                 self.relpath)
    file_list = []
    scm.update(options, None, file_list)
    gclient_scm.os.remove(file_path)
    file_list = []
    scm.revert(options, self.args, file_list)
    self.assertEqual(file_list, [file_path])
    file_list = []
    scm.diff(options, self.args, file_list)
    self.assertEqual(file_list, [])
    sys.stdout.close()

  def testRevertNone(self):
    if not self.enabled:
      return
    options = self.Options()
    scm = gclient_scm.GitWrapper(self.url, self.root_dir,
                                 self.relpath)
    file_list = []
    scm.update(options, None, file_list)
    file_list = []
    scm.revert(options, self.args, file_list)
    self.assertEqual(file_list, [])
    self.assertEqual(scm.revinfo(options, self.args, None),
                     'a7142dc9f0009350b96a11f372b6ea658592aa95')
    sys.stdout.close()

  def testRevertModified(self):
    if not self.enabled:
      return
    options = self.Options()
    scm = gclient_scm.GitWrapper(self.url, self.root_dir,
                                 self.relpath)
    file_list = []
    scm.update(options, None, file_list)
    file_path = join(self.base_path, 'a')
    with open(file_path, 'a') as f:
      f.writelines('touched\n')
    file_list = []
    scm.revert(options, self.args, file_list)
    self.assertEqual(file_list, [file_path])
    file_list = []
    scm.diff(options, self.args, file_list)
    self.assertEqual(file_list, [])
    self.assertEqual(scm.revinfo(options, self.args, None),
                      'a7142dc9f0009350b96a11f372b6ea658592aa95')
    sys.stdout.close()

  def testRevertNew(self):
    if not self.enabled:
      return
    options = self.Options()
    scm = gclient_scm.GitWrapper(self.url, self.root_dir,
                                 self.relpath)
    file_list = []
    scm.update(options, None, file_list)
    file_path = join(self.base_path, 'c')
    with open(file_path, 'w') as f:
      f.writelines('new\n')
    Popen(['git', 'add', 'c'], stdout=PIPE,
          stderr=STDOUT, cwd=self.base_path).communicate()
    file_list = []
    scm.revert(options, self.args, file_list)
    self.assertEqual(file_list, [file_path])
    file_list = []
    scm.diff(options, self.args, file_list)
    self.assertEqual(file_list, [])
    self.assertEqual(scm.revinfo(options, self.args, None),
                     'a7142dc9f0009350b96a11f372b6ea658592aa95')
    sys.stdout.close()

  def testStatusNew(self):
    if not self.enabled:
      return
    options = self.Options()
    file_path = join(self.base_path, 'a')
    with open(file_path, 'a') as f:
      f.writelines('touched\n')
    scm = gclient_scm.GitWrapper(self.url, self.root_dir,
                                 self.relpath)
    file_list = []
    scm.status(options, self.args, file_list)
    self.assertEqual(file_list, [file_path])
    self.checkstdout(
        ('\n________ running \'git -c core.quotePath=false diff --name-status '
         '069c602044c5388d2d15c3f875b057c852003458\' in \'%s\'\n\nM\ta\n') %
            join(self.root_dir, '.'))

  def testStatus2New(self):
    if not self.enabled:
      return
    options = self.Options()
    expected_file_list = []
    for f in ['a', 'b']:
      file_path = join(self.base_path, f)
      with open(file_path, 'a') as f:
        f.writelines('touched\n')
      expected_file_list.extend([file_path])
    scm = gclient_scm.GitWrapper(self.url, self.root_dir,
                                 self.relpath)
    file_list = []
    scm.status(options, self.args, file_list)
    expected_file_list = [join(self.base_path, x) for x in ['a', 'b']]
    self.assertEqual(sorted(file_list), expected_file_list)
    self.checkstdout(
        ('\n________ running \'git -c core.quotePath=false diff --name-status '
         '069c602044c5388d2d15c3f875b057c852003458\' in \'%s\'\n\nM\ta\nM\tb\n')
            % join(self.root_dir, '.'))

  def testUpdateUpdate(self):
    if not self.enabled:
      return
    options = self.Options()
    expected_file_list = [join(self.base_path, x) for x in ['a', 'b']]
    scm = gclient_scm.GitWrapper(self.url, self.root_dir,
                                 self.relpath)
    file_list = []
    scm.update(options, (), file_list)
    self.assertEqual(file_list, expected_file_list)
    self.assertEqual(scm.revinfo(options, (), None),
                      'a7142dc9f0009350b96a11f372b6ea658592aa95')
    sys.stdout.close()

  def testUpdateMerge(self):
    if not self.enabled:
      return
    options = self.Options()
    options.merge = True
    scm = gclient_scm.GitWrapper(self.url, self.root_dir,
                                 self.relpath)
    scm._Run(['checkout', '-q', 'feature'], options)
    rev = scm.revinfo(options, (), None)
    file_list = []
    scm.update(options, (), file_list)
    self.assertEqual(file_list, [join(self.base_path, x)
                                 for x in ['a', 'b', 'c']])
    # The actual commit that is created is unstable, so we verify its tree and
    # parents instead.
    self.assertEqual(scm._Capture(['rev-parse', 'HEAD:']),
                     'd2e35c10ac24d6c621e14a1fcadceb533155627d')
    self.assertEqual(scm._Capture(['rev-parse', 'HEAD^1']), rev)
    self.assertEqual(scm._Capture(['rev-parse', 'HEAD^2']),
                     scm._Capture(['rev-parse', 'origin/master']))
    sys.stdout.close()

  def testUpdateRebase(self):
    if not self.enabled:
      return
    options = self.Options()
    scm = gclient_scm.GitWrapper(self.url, self.root_dir,
                                 self.relpath)
    scm._Run(['checkout', '-q', 'feature'], options)
    file_list = []
    # Fake a 'y' key press.
    scm._AskForData = self._GetAskForDataCallback(
        'Cannot fast-forward merge, attempt to rebase? '
        '(y)es / (q)uit / (s)kip : ', 'y')
    scm.update(options, (), file_list)
    self.assertEqual(file_list, [join(self.base_path, x)
                                 for x in ['a', 'b', 'c']])
    # The actual commit that is created is unstable, so we verify its tree and
    # parent instead.
    self.assertEqual(scm._Capture(['rev-parse', 'HEAD:']),
                     'd2e35c10ac24d6c621e14a1fcadceb533155627d')
    self.assertEqual(scm._Capture(['rev-parse', 'HEAD^']),
                     scm._Capture(['rev-parse', 'origin/master']))
    sys.stdout.close()

  def testUpdateReset(self):
    if not self.enabled:
      return
    options = self.Options()
    options.reset = True

    dir_path = join(self.base_path, 'c')
    os.mkdir(dir_path)
    with open(join(dir_path, 'nested'), 'w') as f:
      f.writelines('new\n')

    file_path = join(self.base_path, 'file')
    with open(file_path, 'w') as f:
      f.writelines('new\n')

    scm = gclient_scm.GitWrapper(self.url, self.root_dir,
                                 self.relpath)
    file_list = []
    scm.update(options, (), file_list)
    self.assert_(gclient_scm.os.path.isdir(dir_path))
    self.assert_(gclient_scm.os.path.isfile(file_path))
    sys.stdout.close()

  def testUpdateResetUnsetsFetchConfig(self):
    if not self.enabled:
      return
    options = self.Options()
    options.reset = True

    scm = gclient_scm.GitWrapper(self.url, self.root_dir,
                                 self.relpath)
    scm._Run(['config', 'remote.origin.fetch',
              '+refs/heads/bad/ref:refs/remotes/origin/bad/ref'], options)

    file_list = []
    scm.update(options, (), file_list)
    self.assertEqual(scm.revinfo(options, (), None),
                     '069c602044c5388d2d15c3f875b057c852003458')
    sys.stdout.close()

  def testUpdateResetDeleteUnversionedTrees(self):
    if not self.enabled:
      return
    options = self.Options()
    options.reset = True
    options.delete_unversioned_trees = True

    dir_path = join(self.base_path, 'dir')
    os.mkdir(dir_path)
    with open(join(dir_path, 'nested'), 'w') as f:
      f.writelines('new\n')

    file_path = join(self.base_path, 'file')
    with open(file_path, 'w') as f:
      f.writelines('new\n')

    scm = gclient_scm.GitWrapper(self.url, self.root_dir,
                                 self.relpath)
    file_list = []
    scm.update(options, (), file_list)
    self.assert_(not gclient_scm.os.path.isdir(dir_path))
    self.assert_(gclient_scm.os.path.isfile(file_path))
    sys.stdout.close()

  def testUpdateUnstagedConflict(self):
    if not self.enabled:
      return
    options = self.Options()
    scm = gclient_scm.GitWrapper(self.url, self.root_dir,
                                 self.relpath)
    file_path = join(self.base_path, 'b')
    with open(file_path, 'w') as f:
      f.writelines('conflict\n')
    try:
      scm.update(options, (), [])
      self.fail()
    except (gclient_scm.gclient_utils.Error, subprocess2.CalledProcessError):
      # The exact exception text varies across git versions so it's not worth
      # verifying it. It's fine as long as it throws.
      pass
    # Manually flush stdout since we can't verify it's content accurately across
    # git versions.
    sys.stdout.getvalue()
    sys.stdout.close()

  @unittest.skip('Skipping until crbug.com/670884 is resolved.')
  def testUpdateLocked(self):
    if not self.enabled:
      return
    options = self.Options()
    scm = gclient_scm.GitWrapper(self.url, self.root_dir,
                                 self.relpath)
    file_path = join(self.base_path, '.git', 'index.lock')
    with open(file_path, 'w'):
      pass
    with self.assertRaises(subprocess2.CalledProcessError):
      scm.update(options, (), [])
    sys.stdout.close()

  def testUpdateLockedBreak(self):
    if not self.enabled:
      return
    options = self.Options()
    options.break_repo_locks = True
    scm = gclient_scm.GitWrapper(self.url, self.root_dir,
                                 self.relpath)
    file_path = join(self.base_path, '.git', 'index.lock')
    with open(file_path, 'w'):
      pass
    scm.update(options, (), [])
    self.assertRegexpMatches(sys.stdout.getvalue(),
                             "breaking lock.*\.git/index\.lock")
    self.assertFalse(os.path.exists(file_path))
    sys.stdout.close()

  def testUpdateConflict(self):
    if not self.enabled:
      return
    options = self.Options()
    scm = gclient_scm.GitWrapper(self.url, self.root_dir,
                                 self.relpath)
    file_path = join(self.base_path, 'b')
    with open(file_path, 'w') as f:
      f.writelines('conflict\n')
    scm._Run(['commit', '-am', 'test'], options)
    scm._AskForData = self._GetAskForDataCallback(
        'Cannot fast-forward merge, attempt to rebase? '
        '(y)es / (q)uit / (s)kip : ', 'y')

    with self.assertRaises(gclient_scm.gclient_utils.Error) as e:
      scm.update(options, (), [])
    self.assertEqual(
        e.exception.args[0],
        'Conflict while rebasing this branch.\n'
        'Fix the conflict and run gclient again.\n'
        'See \'man git-rebase\' for details.\n')

    with self.assertRaises(gclient_scm.gclient_utils.Error) as e:
      scm.update(options, (), [])
    self.assertEqual(
        e.exception.args[0],
        '\n____ . at refs/remotes/origin/master\n'
        '\tYou have unstaged changes.\n'
        '\tPlease commit, stash, or reset.\n')

    sys.stdout.close()

  def testRevinfo(self):
    if not self.enabled:
      return
    options = self.Options()
    scm = gclient_scm.GitWrapper(self.url, self.root_dir,
                                 self.relpath)
    rev_info = scm.revinfo(options, (), None)
    self.assertEqual(rev_info, '069c602044c5388d2d15c3f875b057c852003458')

  def testMirrorPushUrl(self):
    if not self.enabled:
      return
    fakes = fake_repos.FakeRepos()
    fakes.set_up_git()
    self.url = fakes.git_base + 'repo_1'
    self.root_dir = fakes.root_dir
    self.addCleanup(fake_repos.FakeRepos.tear_down_git, fakes)

    mirror = tempfile.mkdtemp()
    self.addCleanup(rmtree, mirror)

    # This should never happen, but if it does, it'd render the other assertions
    # in this test meaningless.
    self.assertFalse(self.url.startswith(mirror))

    git_cache.Mirror.SetCachePath(mirror)
    self.addCleanup(git_cache.Mirror.SetCachePath, None)

    options = self.Options()
    scm = gclient_scm.GitWrapper(self.url, self.root_dir, self.relpath)
    self.assertIsNotNone(scm._GetMirror(self.url, options))
    scm.update(options, (), [])

    fetch_url = scm._Capture(['remote', 'get-url', 'origin'])
    self.assertTrue(
        fetch_url.startswith(mirror),
        msg='\n'.join([
            'Repository fetch url should be in the git cache mirror directory.',
            '  fetch_url: %s' % fetch_url,
            '  mirror:    %s' % mirror]))
    push_url = scm._Capture(['remote', 'get-url', '--push', 'origin'])
    self.assertEqual(push_url, self.url)
    sys.stdout.close()


class ManagedGitWrapperTestCaseMock(unittest.TestCase):
  class OptionsObject(object):
    def __init__(self, verbose=False, revision=None, force=False):
      self.verbose = verbose
      self.revision = revision
      self.deps_os = None
      self.force = force
      self.reset = False
      self.nohooks = False
      self.break_repo_locks = False
      # TODO(maruel): Test --jobs > 1.
      self.jobs = 1
      self.patch_ref = None
      self.patch_repo = None
      self.rebase_patch_ref = True

  def Options(self, *args, **kwargs):
    return self.OptionsObject(*args, **kwargs)

  def checkstdout(self, expected):
    value = sys.stdout.getvalue()
    sys.stdout.close()
    # pylint: disable=no-member
    self.assertEqual(expected, strip_timestamps(value))

  def setUp(self):
    self.fake_hash_1 = 't0ta11yf4k3'
    self.fake_hash_2 = '3v3nf4k3r'
    self.url = 'git://foo'
    self.root_dir = '/tmp' if sys.platform != 'win32' else 't:\\tmp'
    self.relpath = 'fake'
    self.base_path = os.path.join(self.root_dir, self.relpath)
    self.backup_base_path = os.path.join(self.root_dir,
                                         'old_%s.git' % self.relpath)
    mock.patch('gclient_scm.scm.GIT.ApplyEnvVars').start()
    mock.patch('gclient_scm.GitWrapper._CheckMinVersion').start()
    mock.patch('gclient_scm.GitWrapper._Fetch').start()
    mock.patch('gclient_scm.GitWrapper._DeleteOrMove').start()
    mock.patch('sys.stdout', StringIO()).start()
    self.addCleanup(mock.patch.stopall)

  @mock.patch('scm.GIT.IsValidRevision')
  @mock.patch('os.path.isdir', lambda _: True)
  def testGetUsableRevGit(self, mockIsValidRevision):
    # pylint: disable=no-member
    options = self.Options(verbose=True)

    mockIsValidRevision.side_effect = lambda cwd, rev: rev != '1'

    git_scm = gclient_scm.GitWrapper(self.url, self.root_dir,
                                     self.relpath)
    # A [fake] git sha1 with a git repo should work (this is in the case that
    # the LKGR gets flipped to git sha1's some day).
    self.assertEqual(git_scm.GetUsableRev(self.fake_hash_1, options),
                     self.fake_hash_1)
    # An SVN rev with an existing purely git repo should raise an exception.
    self.assertRaises(gclient_scm.gclient_utils.Error,
                      git_scm.GetUsableRev, '1', options)

  @mock.patch('gclient_scm.GitWrapper._Clone')
  @mock.patch('os.path.isdir')
  @mock.patch('os.path.exists')
  @mock.patch('subprocess2.check_output')
  def testUpdateNoDotGit(
      self, mockCheckOutput, mockExists, mockIsdir, mockClone):
    mockIsdir.side_effect = lambda path: path == self.base_path
    mockExists.side_effect = lambda path: path == self.base_path
    mockCheckOutput.return_value = b''

    options = self.Options()
    scm = gclient_scm.GitWrapper(
        self.url, self.root_dir, self.relpath)
    scm.update(options, None, [])

    env = gclient_scm.scm.GIT.ApplyEnvVars({})
    self.assertEqual(
        mockCheckOutput.mock_calls,
        [
            mock.call(
                ['git', '-c', 'core.quotePath=false', 'ls-files'],
                cwd=self.base_path, env=env, stderr=-1),
            mock.call(
                ['git', 'rev-parse', '--verify', 'HEAD'],
                cwd=self.base_path, env=env, stderr=-1),
        ])
    mockClone.assert_called_with(
        'refs/remotes/origin/master', self.url, options)
    self.checkstdout('\n')

  @mock.patch('gclient_scm.GitWrapper._Clone')
  @mock.patch('os.path.isdir')
  @mock.patch('os.path.exists')
  @mock.patch('subprocess2.check_output')
  def testUpdateConflict(
      self, mockCheckOutput, mockExists, mockIsdir, mockClone):
    mockIsdir.side_effect = lambda path: path == self.base_path
    mockExists.side_effect = lambda path: path == self.base_path
    mockCheckOutput.return_value = b''
    mockClone.side_effect = [
        gclient_scm.subprocess2.CalledProcessError(
            None, None, None, None, None),
        None,
    ]

    options = self.Options()
    scm = gclient_scm.GitWrapper(self.url, self.root_dir,
                            self.relpath)
    scm.update(options, None, [])

    env = gclient_scm.scm.GIT.ApplyEnvVars({})
    self.assertEqual(
        mockCheckOutput.mock_calls,
        [
            mock.call(
                ['git', '-c', 'core.quotePath=false', 'ls-files'],
                cwd=self.base_path, env=env, stderr=-1),
            mock.call(
                ['git', 'rev-parse', '--verify', 'HEAD'],
                cwd=self.base_path, env=env, stderr=-1),
        ])
    mockClone.assert_called_with(
        'refs/remotes/origin/master', self.url, options)
    self.checkstdout('\n')


class UnmanagedGitWrapperTestCase(BaseGitWrapperTestCase):
  def checkInStdout(self, expected):
    value = sys.stdout.getvalue()
    sys.stdout.close()
    # pylint: disable=no-member
    self.assertIn(expected, value)

  def checkNotInStdout(self, expected):
    value = sys.stdout.getvalue()
    sys.stdout.close()
    # pylint: disable=no-member
    self.assertNotIn(expected, value)

  def getCurrentBranch(self):
    # Returns name of current branch or HEAD for detached HEAD
    branch = gclient_scm.scm.GIT.Capture(['rev-parse', '--abbrev-ref', 'HEAD'],
                                          cwd=self.base_path)
    if branch == 'HEAD':
      return None
    return branch

  def testUpdateClone(self):
    if not self.enabled:
      return
    options = self.Options()

    origin_root_dir = self.root_dir
    self.root_dir = tempfile.mkdtemp()
    self.relpath = '.'
    self.base_path = join(self.root_dir, self.relpath)

    scm = gclient_scm.GitWrapper(origin_root_dir,
                                 self.root_dir,
                                 self.relpath)

    expected_file_list = [join(self.base_path, "a"),
                          join(self.base_path, "b")]
    file_list = []
    options.revision = 'unmanaged'
    scm.update(options, (), file_list)

    self.assertEqual(file_list, expected_file_list)
    self.assertEqual(scm.revinfo(options, (), None),
                     '069c602044c5388d2d15c3f875b057c852003458')
    # indicates detached HEAD
    self.assertEqual(self.getCurrentBranch(), None)
    self.checkInStdout(
      'Checked out refs/remotes/origin/master to a detached HEAD')

    rmtree(origin_root_dir)

  def testUpdateCloneOnCommit(self):
    if not self.enabled:
      return
    options = self.Options()

    origin_root_dir = self.root_dir
    self.root_dir = tempfile.mkdtemp()
    self.relpath = '.'
    self.base_path = join(self.root_dir, self.relpath)
    url_with_commit_ref = origin_root_dir +\
                          '@a7142dc9f0009350b96a11f372b6ea658592aa95'

    scm = gclient_scm.GitWrapper(url_with_commit_ref,
                                 self.root_dir,
                                 self.relpath)

    expected_file_list = [join(self.base_path, "a"),
                          join(self.base_path, "b")]
    file_list = []
    options.revision = 'unmanaged'
    scm.update(options, (), file_list)

    self.assertEqual(file_list, expected_file_list)
    self.assertEqual(scm.revinfo(options, (), None),
                     'a7142dc9f0009350b96a11f372b6ea658592aa95')
    # indicates detached HEAD
    self.assertEqual(self.getCurrentBranch(), None)
    self.checkInStdout(
      'Checked out a7142dc9f0009350b96a11f372b6ea658592aa95 to a detached HEAD')

    rmtree(origin_root_dir)

  def testUpdateCloneOnBranch(self):
    if not self.enabled:
      return
    options = self.Options()

    origin_root_dir = self.root_dir
    self.root_dir = tempfile.mkdtemp()
    self.relpath = '.'
    self.base_path = join(self.root_dir, self.relpath)
    url_with_branch_ref = origin_root_dir + '@feature'

    scm = gclient_scm.GitWrapper(url_with_branch_ref,
                                 self.root_dir,
                                 self.relpath)

    expected_file_list = [join(self.base_path, "a"),
                          join(self.base_path, "b"),
                          join(self.base_path, "c")]
    file_list = []
    options.revision = 'unmanaged'
    scm.update(options, (), file_list)

    self.assertEqual(file_list, expected_file_list)
    self.assertEqual(scm.revinfo(options, (), None),
                     '9a51244740b25fa2ded5252ca00a3178d3f665a9')
    # indicates detached HEAD
    self.assertEqual(self.getCurrentBranch(), None)
    self.checkInStdout(
        'Checked out 9a51244740b25fa2ded5252ca00a3178d3f665a9 '
        'to a detached HEAD')

    rmtree(origin_root_dir)

  def testUpdateCloneOnFetchedRemoteBranch(self):
    if not self.enabled:
      return
    options = self.Options()

    origin_root_dir = self.root_dir
    self.root_dir = tempfile.mkdtemp()
    self.relpath = '.'
    self.base_path = join(self.root_dir, self.relpath)
    url_with_branch_ref = origin_root_dir + '@refs/remotes/origin/feature'

    scm = gclient_scm.GitWrapper(url_with_branch_ref,
                                 self.root_dir,
                                 self.relpath)

    expected_file_list = [join(self.base_path, "a"),
                          join(self.base_path, "b"),
                          join(self.base_path, "c")]
    file_list = []
    options.revision = 'unmanaged'
    scm.update(options, (), file_list)

    self.assertEqual(file_list, expected_file_list)
    self.assertEqual(scm.revinfo(options, (), None),
                     '9a51244740b25fa2ded5252ca00a3178d3f665a9')
    # indicates detached HEAD
    self.assertEqual(self.getCurrentBranch(), None)
    self.checkInStdout(
      'Checked out refs/remotes/origin/feature to a detached HEAD')

    rmtree(origin_root_dir)

  def testUpdateCloneOnTrueRemoteBranch(self):
    if not self.enabled:
      return
    options = self.Options()

    origin_root_dir = self.root_dir
    self.root_dir = tempfile.mkdtemp()
    self.relpath = '.'
    self.base_path = join(self.root_dir, self.relpath)
    url_with_branch_ref = origin_root_dir + '@refs/heads/feature'

    scm = gclient_scm.GitWrapper(url_with_branch_ref,
                                 self.root_dir,
                                 self.relpath)

    expected_file_list = [join(self.base_path, "a"),
                          join(self.base_path, "b"),
                          join(self.base_path, "c")]
    file_list = []
    options.revision = 'unmanaged'
    scm.update(options, (), file_list)

    self.assertEqual(file_list, expected_file_list)
    self.assertEqual(scm.revinfo(options, (), None),
                     '9a51244740b25fa2ded5252ca00a3178d3f665a9')
    # @refs/heads/feature is AKA @refs/remotes/origin/feature in the clone, so
    # should be treated as such by gclient.
    # TODO(mmoss): Though really, we should only allow DEPS to specify branches
    # as they are known in the upstream repo, since the mapping into the local
    # repo can be modified by users (or we might even want to change the gclient
    # defaults at some point). But that will take more work to stop using
    # refs/remotes/ everywhere that we do (and to stop assuming a DEPS ref will
    # always resolve locally, like when passing them to show-ref or rev-list).
    self.assertEqual(self.getCurrentBranch(), None)
    self.checkInStdout(
      'Checked out refs/remotes/origin/feature to a detached HEAD')

    rmtree(origin_root_dir)

  def testUpdateUpdate(self):
    if not self.enabled:
      return
    options = self.Options()
    expected_file_list = []
    scm = gclient_scm.GitWrapper(self.url, self.root_dir,
                                 self.relpath)
    file_list = []
    options.revision = 'unmanaged'
    scm.update(options, (), file_list)
    self.assertEqual(file_list, expected_file_list)
    self.assertEqual(scm.revinfo(options, (), None),
                     '069c602044c5388d2d15c3f875b057c852003458')
    self.checkstdout('________ unmanaged solution; skipping .\n')


class CipdWrapperTestCase(unittest.TestCase):

  def setUp(self):
    # Create this before setting up mocks.
    self._cipd_root_dir = tempfile.mkdtemp()
    self._workdir = tempfile.mkdtemp()

    self._cipd_instance_url = 'https://chrome-infra-packages.appspot.com'
    self._cipd_root = gclient_scm.CipdRoot(
        self._cipd_root_dir,
        self._cipd_instance_url)
    self._cipd_packages = [
        self._cipd_root.add_package('f', 'foo_package', 'foo_version'),
        self._cipd_root.add_package('b', 'bar_package', 'bar_version'),
        self._cipd_root.add_package('b', 'baz_package', 'baz_version'),
    ]
    mock.patch('tempfile.mkdtemp', lambda: self._workdir).start()
    mock.patch('gclient_scm.CipdRoot.add_package').start()
    mock.patch('gclient_scm.CipdRoot.clobber').start()
    mock.patch('gclient_scm.CipdRoot.ensure').start()
    self.addCleanup(mock.patch.stopall)

  def tearDown(self):
    rmtree(self._cipd_root_dir)
    rmtree(self._workdir)

  def createScmWithPackageThatSatisfies(self, condition):
    return gclient_scm.CipdWrapper(
        url=self._cipd_instance_url,
        root_dir=self._cipd_root_dir,
        relpath='fake_relpath',
        root=self._cipd_root,
        package=self.getPackageThatSatisfies(condition))

  def getPackageThatSatisfies(self, condition):
    for p in self._cipd_packages:
      if condition(p):
        return p

    self.fail('Unable to find a satisfactory package.')

  def testRevert(self):
    """Checks that revert does nothing."""
    scm = self.createScmWithPackageThatSatisfies(lambda _: True)
    scm.revert(None, (), [])

  @mock.patch('gclient_scm.gclient_utils.CheckCallAndFilter')
  @mock.patch('gclient_scm.gclient_utils.rmtree')
  def testRevinfo(self, mockRmtree, mockCheckCallAndFilter):
    """Checks that revinfo uses the JSON from cipd describe."""
    scm = self.createScmWithPackageThatSatisfies(lambda _: True)

    expected_revinfo = '0123456789abcdef0123456789abcdef01234567'
    json_contents = {
        'result': {
            'pin': {
                'instance_id': expected_revinfo,
            }
        }
    }
    describe_json_path = join(self._workdir, 'describe.json')
    with open(describe_json_path, 'w') as describe_json:
      json.dump(json_contents, describe_json)

    revinfo = scm.revinfo(None, (), [])
    self.assertEqual(revinfo, expected_revinfo)

    mockRmtree.assert_called_with(self._workdir)
    mockCheckCallAndFilter.assert_called_with([
        'cipd', 'describe', 'foo_package',
        '-log-level', 'error',
        '-version', 'foo_version',
        '-json-output', describe_json_path,
    ])

  def testUpdate(self):
    """Checks that update does nothing."""
    scm = self.createScmWithPackageThatSatisfies(lambda _: True)
    scm.update(None, (), [])


class GerritChangesFakeRepo(fake_repos.FakeReposBase):
  def populateGit(self):
    # Creates a tree that looks like this:
    #
    #       6 refs/changes/35/1235/1
    #       |
    #       5 refs/changes/34/1234/1
    #       |
    # 1--2--3--4 refs/heads/master
    #    |  |
    #    |  11(5)--12 refs/heads/master-with-5
    #    |
    #    7--8--9 refs/heads/feature
    #       |
    #       10 refs/changes/36/1236/1
    #

    self._commit_git('repo_1', {'commit 1': 'touched'})
    self._commit_git('repo_1', {'commit 2': 'touched'})
    self._commit_git('repo_1', {'commit 3': 'touched'})
    self._commit_git('repo_1', {'commit 4': 'touched'})
    self._create_ref('repo_1', 'refs/heads/master', 4)

    # Create a change on top of commit 3 that consists of two commits.
    self._commit_git('repo_1',
                     {'commit 5': 'touched',
                      'change': '1234'},
                     base=3)
    self._create_ref('repo_1', 'refs/changes/34/1234/1', 5)
    self._commit_git('repo_1',
                     {'commit 6': 'touched',
                      'change': '1235'})
    self._create_ref('repo_1', 'refs/changes/35/1235/1', 6)

    # Create a refs/heads/feature branch on top of commit 2, consisting of three
    # commits.
    self._commit_git('repo_1', {'commit 7': 'touched'}, base=2)
    self._commit_git('repo_1', {'commit 8': 'touched'})
    self._commit_git('repo_1', {'commit 9': 'touched'})
    self._create_ref('repo_1', 'refs/heads/feature', 9)

    # Create a change of top of commit 8.
    self._commit_git('repo_1',
                     {'commit 10': 'touched',
                      'change': '1236'},
                     base=8)
    self._create_ref('repo_1', 'refs/changes/36/1236/1', 10)

    # Create a refs/heads/master-with-5 on top of commit 3 which is a branch
    # where refs/changes/34/1234/1 (commit 5) has already landed as commit 11.
    self._commit_git('repo_1',
                     # This is really commit 11, but has the changes of commit 5
                     {'commit 5': 'touched',
                      'change': '1234'},
                     base=3)
    self._commit_git('repo_1', {'commit 12': 'touched'})
    self._create_ref('repo_1', 'refs/heads/master-with-5', 12)


class GerritChangesTest(fake_repos.FakeReposTestBase):
  FAKE_REPOS_CLASS = GerritChangesFakeRepo

  def setUp(self):
    super(GerritChangesTest, self).setUp()
    self.enabled = self.FAKE_REPOS.set_up_git()
    self.options = BaseGitWrapperTestCase.OptionsObject()
    self.url = self.git_base + 'repo_1'
    self.mirror = None

  def setUpMirror(self):
    self.mirror = tempfile.mkdtemp()
    git_cache.Mirror.SetCachePath(self.mirror)
    self.addCleanup(rmtree, self.mirror)
    self.addCleanup(git_cache.Mirror.SetCachePath, None)

  def assertCommits(self, commits):
    """Check that all, and only |commits| are present in the current checkout.
    """
    for i in commits:
      name = os.path.join(self.root_dir, 'commit ' + str(i))
      self.assertTrue(os.path.exists(name), 'Commit not found: %s' % name)

    all_commits = set(range(1, len(self.FAKE_REPOS.git_hashes['repo_1'])))
    for i in all_commits - set(commits):
      name = os.path.join(self.root_dir, 'commit ' + str(i))
      self.assertFalse(os.path.exists(name), 'Unexpected commit: %s' % name)

  def testCanCloneGerritChange(self):
    scm = gclient_scm.GitWrapper(self.url, self.root_dir, '.')
    file_list = []

    self.options.revision = 'refs/changes/35/1235/1'
    scm.update(self.options, None, file_list)
    self.assertEqual(self.githash('repo_1', 6), self.gitrevparse(self.root_dir))

  def testCanSyncToGerritChange(self):
    scm = gclient_scm.GitWrapper(self.url, self.root_dir, '.')
    file_list = []

    self.options.revision = self.githash('repo_1', 1)
    scm.update(self.options, None, file_list)
    self.assertEqual(self.githash('repo_1', 1), self.gitrevparse(self.root_dir))

    self.options.revision = 'refs/changes/35/1235/1'
    scm.update(self.options, None, file_list)
    self.assertEqual(self.githash('repo_1', 6), self.gitrevparse(self.root_dir))

  def testCanCloneGerritChangeMirror(self):
    self.setUpMirror()
    self.testCanCloneGerritChange()

  def testCanSyncToGerritChangeMirror(self):
    self.setUpMirror()
    self.testCanSyncToGerritChange()

  def testAppliesPatchOnTopOfMasterByDefault(self):
    """Test the default case, where we apply a patch on top of master."""
    scm = gclient_scm.GitWrapper(self.url, self.root_dir, '.')
    file_list = []

    # Make sure we don't specify a revision.
    self.options.revision = None
    scm.update(self.options, None, file_list)
    self.assertEqual(self.githash('repo_1', 4), self.gitrevparse(self.root_dir))

    scm.apply_patch_ref(
        self.url, 'refs/changes/35/1235/1', 'refs/heads/master', self.options,
        file_list)

    self.assertCommits([1, 2, 3, 4, 5, 6])
    self.assertEqual(self.githash('repo_1', 4), self.gitrevparse(self.root_dir))

  def testCheckoutOlderThanPatchBase(self):
    """Test applying a patch on an old checkout.

    We first checkout commit 1, and try to patch refs/changes/35/1235/1, which
    contains commits 5 and 6, and is based on top of commit 3.
    The final result should contain commits 1, 5 and 6, but not commits 2 or 3.
    """
    scm = gclient_scm.GitWrapper(self.url, self.root_dir, '.')
    file_list = []

    # Sync to commit 1
    self.options.revision = self.githash('repo_1', 1)
    scm.update(self.options, None, file_list)
    self.assertEqual(self.githash('repo_1', 1), self.gitrevparse(self.root_dir))

    # Apply the change on top of that.
    scm.apply_patch_ref(
        self.url, 'refs/changes/35/1235/1', 'refs/heads/master', self.options,
        file_list)

    self.assertCommits([1, 5, 6])
    self.assertEqual(self.githash('repo_1', 1), self.gitrevparse(self.root_dir))

  def testCheckoutOriginFeature(self):
    """Tests that we can apply a patch on a branch other than master."""
    scm = gclient_scm.GitWrapper(self.url, self.root_dir, '.')
    file_list = []

    # Sync to remote's refs/heads/feature
    self.options.revision = 'refs/heads/feature'
    scm.update(self.options, None, file_list)
    self.assertEqual(self.githash('repo_1', 9), self.gitrevparse(self.root_dir))

    # Apply the change on top of that.
    scm.apply_patch_ref(
        self.url, 'refs/changes/36/1236/1', 'refs/heads/feature', self.options,
        file_list)

    self.assertCommits([1, 2, 7, 8, 9, 10])
    self.assertEqual(self.githash('repo_1', 9), self.gitrevparse(self.root_dir))

  def testCheckoutOriginFeatureOnOldRevision(self):
    """Tests that we can apply a patch on an old checkout, on a branch other
    than master."""
    scm = gclient_scm.GitWrapper(self.url, self.root_dir, '.')
    file_list = []

    # Sync to remote's refs/heads/feature on an old revision
    self.options.revision = self.githash('repo_1', 7)
    scm.update(self.options, None, file_list)
    self.assertEqual(self.githash('repo_1', 7), self.gitrevparse(self.root_dir))

    # Apply the change on top of that.
    scm.apply_patch_ref(
        self.url, 'refs/changes/36/1236/1', 'refs/heads/feature', self.options,
        file_list)

    # We shouldn't have rebased on top of 2 (which is the merge base between
    # remote's master branch and the change) but on top of 7 (which is the
    # merge base between remote's feature branch and the change).
    self.assertCommits([1, 2, 7, 10])
    self.assertEqual(self.githash('repo_1', 7), self.gitrevparse(self.root_dir))

  def testCheckoutOriginFeaturePatchBranch(self):
    scm = gclient_scm.GitWrapper(self.url, self.root_dir, '.')
    file_list = []

    # Sync to the hash instead of remote's refs/heads/feature.
    self.options.revision = self.githash('repo_1', 9)
    scm.update(self.options, None, file_list)
    self.assertEqual(self.githash('repo_1', 9), self.gitrevparse(self.root_dir))

    # Apply refs/changes/34/1234/1, created for remote's master branch on top of
    # remote's feature branch.
    scm.apply_patch_ref(
        self.url, 'refs/changes/35/1235/1', 'refs/heads/master', self.options,
        file_list)

    # Commits 5 and 6 are part of the patch, and commits 1, 2, 7, 8 and 9 are
    # part of remote's feature branch.
    self.assertCommits([1, 2, 5, 6, 7, 8, 9])
    self.assertEqual(self.githash('repo_1', 9), self.gitrevparse(self.root_dir))

  def testDoesntRebasePatchMaster(self):
    """Tests that we can apply a patch without rebasing it.
    """
    scm = gclient_scm.GitWrapper(self.url, self.root_dir, '.')
    file_list = []

    self.options.rebase_patch_ref = False
    scm.update(self.options, None, file_list)
    self.assertEqual(self.githash('repo_1', 4), self.gitrevparse(self.root_dir))

    # Apply the change on top of that.
    scm.apply_patch_ref(
        self.url, 'refs/changes/35/1235/1', 'refs/heads/master', self.options,
        file_list)

    self.assertCommits([1, 2, 3, 5, 6])
    self.assertEqual(self.githash('repo_1', 4), self.gitrevparse(self.root_dir))

  def testDoesntRebasePatchOldCheckout(self):
    """Tests that we can apply a patch without rebasing it on an old checkout.
    """
    scm = gclient_scm.GitWrapper(self.url, self.root_dir, '.')
    file_list = []

    # Sync to commit 1
    self.options.revision = self.githash('repo_1', 1)
    self.options.rebase_patch_ref = False
    scm.update(self.options, None, file_list)
    self.assertEqual(self.githash('repo_1', 1), self.gitrevparse(self.root_dir))

    # Apply the change on top of that.
    scm.apply_patch_ref(
        self.url, 'refs/changes/35/1235/1', 'refs/heads/master', self.options,
        file_list)

    self.assertCommits([1, 2, 3, 5, 6])
    self.assertEqual(self.githash('repo_1', 1), self.gitrevparse(self.root_dir))

  def testDoesntSoftResetIfNotAskedTo(self):
    """Test that we can apply a patch without doing a soft reset."""
    scm = gclient_scm.GitWrapper(self.url, self.root_dir, '.')
    file_list = []

    self.options.reset_patch_ref = False
    scm.update(self.options, None, file_list)
    self.assertEqual(self.githash('repo_1', 4), self.gitrevparse(self.root_dir))

    scm.apply_patch_ref(
        self.url, 'refs/changes/35/1235/1', 'refs/heads/master', self.options,
        file_list)

    self.assertCommits([1, 2, 3, 4, 5, 6])
    # The commit hash after cherry-picking is not known, but it must be
    # different from what the repo was synced at before patching.
    self.assertNotEqual(self.githash('repo_1', 4),
                        self.gitrevparse(self.root_dir))

  def testRecoversAfterPatchFailure(self):
    scm = gclient_scm.GitWrapper(self.url, self.root_dir, '.')
    file_list = []

    self.options.revision = 'refs/changes/34/1234/1'
    scm.update(self.options, None, file_list)
    self.assertEqual(self.githash('repo_1', 5), self.gitrevparse(self.root_dir))

    # Checkout 'refs/changes/34/1234/1' modifies the 'change' file, so trying to
    # patch 'refs/changes/36/1236/1' creates a patch failure.
    with self.assertRaises(subprocess2.CalledProcessError) as cm:
      scm.apply_patch_ref(
          self.url, 'refs/changes/36/1236/1', 'refs/heads/master', self.options,
          file_list)
    self.assertEqual(cm.exception.cmd[:2], ['git', 'cherry-pick'])
    self.assertIn(b'error: could not apply', cm.exception.stderr)

    # Try to apply 'refs/changes/35/1235/1', which doesn't have a merge
    # conflict.
    scm.apply_patch_ref(
        self.url, 'refs/changes/35/1235/1', 'refs/heads/master', self.options,
        file_list)
    self.assertCommits([1, 2, 3, 5, 6])
    self.assertEqual(self.githash('repo_1', 5), self.gitrevparse(self.root_dir))

  def testIgnoresAlreadyMergedCommits(self):
    scm = gclient_scm.GitWrapper(self.url, self.root_dir, '.')
    file_list = []

    self.options.revision = 'refs/heads/master-with-5'
    scm.update(self.options, None, file_list)
    self.assertEqual(self.githash('repo_1', 12),
                     self.gitrevparse(self.root_dir))

    # When we try 'refs/changes/35/1235/1' on top of 'refs/heads/feature',
    # 'refs/changes/34/1234/1' will be an empty commit, since the changes were
    # already present in the tree as commit 11.
    # Make sure we deal with this gracefully.
    scm.apply_patch_ref(
        self.url, 'refs/changes/35/1235/1', 'refs/heads/feature', self.options,
        file_list)
    self.assertCommits([1, 2, 3, 5, 6, 12])
    self.assertEqual(self.githash('repo_1', 12),
                     self.gitrevparse(self.root_dir))

  def testRecoversFromExistingCherryPick(self):
    scm = gclient_scm.GitWrapper(self.url, self.root_dir, '.')
    file_list = []

    self.options.revision = 'refs/changes/34/1234/1'
    scm.update(self.options, None, file_list)
    self.assertEqual(self.githash('repo_1', 5), self.gitrevparse(self.root_dir))

    # Checkout 'refs/changes/34/1234/1' modifies the 'change' file, so trying to
    # cherry-pick 'refs/changes/36/1236/1' raises an error.
    scm._Run(['fetch', 'origin', 'refs/changes/36/1236/1'], self.options)
    with self.assertRaises(subprocess2.CalledProcessError) as cm:
      scm._Run(['cherry-pick', 'FETCH_HEAD'], self.options)
    self.assertEqual(cm.exception.cmd[:2], ['git', 'cherry-pick'])

    # Try to apply 'refs/changes/35/1235/1', which doesn't have a merge
    # conflict.
    scm.apply_patch_ref(
        self.url, 'refs/changes/35/1235/1', 'refs/heads/master', self.options,
        file_list)
    self.assertCommits([1, 2, 3, 5, 6])
    self.assertEqual(self.githash('repo_1', 5), self.gitrevparse(self.root_dir))


if __name__ == '__main__':
  level = logging.DEBUG if '-v' in sys.argv else logging.FATAL
  logging.basicConfig(
      level=level,
      format='%(asctime).19s %(levelname)s %(filename)s:'
             '%(lineno)s %(message)s')
  unittest.main()

# vim: ts=2:sw=2:tw=80:et:
