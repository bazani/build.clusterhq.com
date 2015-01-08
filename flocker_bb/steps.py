from twisted.internet import defer
from twisted.python import log
from twisted.python.filepath import FilePath

from buildbot.process.properties import Interpolate
from buildbot.steps.shell import ShellCommand
from buildbot.steps.source.git import Git
from buildbot.steps.source.base import Source
from buildbot.process.factory import BuildFactory
from buildbot.status.results import SUCCESS
from buildbot.process import buildstep
from buildbot.process.properties import renderer

from os import path
import re
import json

VIRTUALENV_DIR = '%(prop:workdir)s/venv'

VIRTUALENV_PY = Interpolate("%(prop:workdir)s/../dependencies/virtualenv.py")
GITHUB = b"https://github.com/ClusterHQ"

TWISTED_GIT = b'https://github.com/twisted/twisted'

def buildVirtualEnv(python, useSystem=False):
    steps = []
    if useSystem:
        command = ['virtualenv', '-p', python]
    else:
        command = [python, VIRTUALENV_PY]
    command += ["--clear", Interpolate(VIRTUALENV_DIR)],
    steps.append(ShellCommand(
            name="build-virtualenv",
            description=["build", "virtualenv"],
            descriptionDone=["built", "virtualenv"],
            command=command,
            haltOnFailure=True,
            ))
    steps.append(ShellCommand(
            name="clean-virtualenv-builds",
            description=["cleaning", "virtualenv"],
            descriptionDone=["clean", "virtualenv"],
            command=["rm", "-rf" , Interpolate(path.join(VIRTUALENV_DIR, "build"))],
            haltOnFailure=True,
            ))
    return steps


def getFactory(codebase, useSubmodules=True, mergeForward=False):
    factory = BuildFactory()

    repourl = GITHUB + b"/" + codebase
    # check out the source
    factory.addStep(
        Git(repourl=repourl,
            submodules=useSubmodules, mode='full', method='fresh',
            codebase=codebase))

    if mergeForward:
        factory.addStep(
            MergeForward(repourl=repourl, codebase=codebase))

    if useSubmodules:
        # Work around http://trac.buildbot.net/ticket/2155
        factory.addStep(
            ShellCommand(command=["git", "submodule", "update", "--init"],
                         description=["updating", "git", "submodules"],
                         descriptionDone=["update", "git", "submodules"],
                         name="update-git-submodules"))

    return factory


@renderer
def buildbotURL(build):
    return build.getBuild().build_status.master.status.getBuildbotURL()


def virtualenvBinary(command):
    return Interpolate(path.join(VIRTUALENV_DIR, "bin", command))


def asJSON(data):
    @renderer
    def render(props):
        return (props.render(data)
                .addCallback(json.dumps, indent=2, separators=(',', ': ')))
    return render



class URLShellCommand(ShellCommand):
    renderables = ["urls"]

    def __init__(self, urls, **kwargs):
        ShellCommand.__init__(self, **kwargs)
        self.urls = urls

    def createSummary(self, log):
        ShellCommand.createSummary(self, log)
        for name, url in self.urls.iteritems():
            self.addURL(name, url)


class MasterWriteFile(buildstep.BuildStep):
    """
    Write a rendered string to a file on the master.
    """
    name = 'MasterWriteFile'
    description = ['writing']
    descriptionDone = ['write']
    renderables = ['content', 'path', 'urls']

    def __init__(self, path, content, urls, **kwargs):
        buildstep.BuildStep.__init__(self, **kwargs)
        self.content = content
        self.path = path
        self.urls = urls

    def start(self):
        path = FilePath(self.path)
        parent = path.parent()
        if not parent.exists():
            parent.makedirs()
        path.setContent(self.content)
        for name, url in self.urls.iteritems():
            self.addURL(name, url)
        self.step_status.setText(self.describe(done=True))
        self.finished(SUCCESS)



class MergeForward(Source):
    """
    Merge with master.
    """
    name = 'merge-forward'
    description = ['merging', 'forward']
    descriptionDone = ['merge', 'forward']
    haltOnFailure = True


    def __init__(self, repourl, branch='master',
            **kwargs):
        self.repourl = repourl
        self.branch = branch
        kwargs['env'] = {
                'GIT_AUTHOR_EMAIL': 'buildbot@clusterhq.com',
                'GIT_AUTHOR_NAME': 'ClusterHQ Buildbot',
                'GIT_COMMITTER_EMAIL': 'buildbot@clusterhq.com',
                'GIT_COMMITTER_NAME': 'ClusterHQ Buildbot',
                }
        Source.__init__(self, **kwargs)
        self.addFactoryArguments(repourl=repourl, branch=branch)


    @staticmethod
    def _isMaster(branch):
        return branch == 'master'

    @staticmethod
    def _isRelease(branch):
        return (branch.startswith('release/') or re.match('^[0-9]+\.[0-9]+\.[0-9]+(?:dev[0-9]+|pre[0-9]+)?$', branch))


    def startVC(self, branch, revision, patch):
        self.stdio_log = self.addLog('stdio')

        self.step_status.setText(['merging', 'forward'])
        d = defer.succeed(None)
        if not self._isMaster(branch):
            d.addCallback(lambda _: self._fetch())
        if not (self._isMaster(branch) or self._isRelease(branch)):
            d.addCallback(self._getCommitDate)
            d.addCallback(self._merge)
        if self._isMaster(branch):
            d.addCallback(lambda _: self._getPreviousVersion())
        else:
            d.addCallback(lambda _: self._getMergeBase())
        d.addCallback(self._setLintVersion)

        d.addCallback(lambda _: SUCCESS)
        d.addCallbacks(self.finished, self.checkDisconnect)
        d.addErrback(self.failed)

    def finished(self, results):
        if results == SUCCESS:
            self.step_status.setText(['merge', 'forward'])
        else:
            self.step_status.setText(['merge', 'forward', 'failed'])
        return Source.finished(self, results)

    def _fetch(self):
        return self._dovccmd(['fetch', self.repourl, 'master'])

    def _merge(self, date):
        # We re-use the date of the latest commit from the branch
        # to ensure that the commit hash is consistent.
        self.env.update({
            'GIT_AUTHOR_DATE': date,
            'GIT_COMMITTER_DATE': date,
        })
        return self._dovccmd(['merge',
                              '--no-ff', '--no-stat',
                              'FETCH_HEAD'])

    def _getPreviousVersion(self):
        return self._dovccmd(['rev-parse', 'HEAD~1'],
                              collectStdout=True)

    def _getMergeBase(self):
        return self._dovccmd(['merge-base', 'HEAD', 'FETCH_HEAD'],
                              collectStdout=True)

    def _setLintVersion(self, version):
        self.setProperty("lint_revision", version.strip(), "merge-forward")

    def _getCommitDate(self, date):
        return self._dovccmd(['log', '--format=%ci', '-n1'], collectStdout=True)

    def _dovccmd(self, command, abandonOnFailure=True, collectStdout=False, extra_args={}):
        cmd = buildstep.RemoteShellCommand(self.workdir, ['git'] + command,
                                           env=self.env,
                                           logEnviron=self.logEnviron,
                                           collectStdout=collectStdout,
                                           **extra_args)
        cmd.useLog(self.stdio_log, False)
        d = self.runCommand(cmd)
        def evaluateCommand(cmd):
            if abandonOnFailure and cmd.rc != 0:
                log.msg("Source step failed while running command %s" % cmd)
                raise buildstep.BuildStepFailed()
            if collectStdout:
                return cmd.stdout
            else:
                return cmd.rc
        d.addCallback(lambda _: evaluateCommand(cmd))
        return d

def pip(what, packages):
    """
    Installs a list of packages with pip, in the current virtualenv.

    @param what: Description of the packages being installed.
    @param packages: L{list} of packages to install
    @returns: L{BuildStep}
    """
    return ShellCommand(
        name="install-" + what,
        description=["installing", what],
        descriptionDone=["install", what],
        command=[Interpolate(path.join(VIRTUALENV_DIR, "bin/pip")),
                 "install",
                 packages,
                 ],
        haltOnFailure=True)


def isBranch(codebase, predicate):
    """
    Return C{doStepIf} function checking whether the built branch
    matches the given branch.

    @param codebase: Codebase to check
    @param predicate: L{callable} that takes a branch and returns whether
        the step should be run.
    """
    def test(step):
        sourcestamp = step.build.getSourceStamp(codebase)
        branch = sourcestamp.branch
        return predicate(branch)
    return test


def isMasterBranch(codebase):
    return isBranch(codebase, MergeForward._isMaster)


def isReleaseBranch(codebase):
    return isBranch(codebase, MergeForward._isRelease)
