# From https://twistedmatrix.com/trac/ticket/5786
def timeoutDeferred(reactor, deferred, seconds):
    """
    Cancel a L{Deferred} if it does not have a result available within the
    given amount of time.
    @see: L{Deferred.cancel}.
    The timeout only waits for callbacks that were added before
    L{timeoutDeferred} was called. If the L{Deferred} is fired then the
    timeout will be removed, even if callbacks added after
    L{timeoutDeferred} are still waiting for a result to become available.
    @type reactor: L{IReactorTime}
    @param reactor: A provider of L{twisted.internet.interfaces.IReactorTime}.
    @type deferred: L{Deferred}
    @param deferred: The L{Deferred} to time out.
    @type seconds: C{float}
    @param seconds: The number of seconds before the timeout will happen.
    @rtype: L{twisted.internet.interfaces.IDelayedCall}
    @return: The scheduled timeout call.
    """
    # Schedule timeout, making sure we know when it happened:
    def timedOutCall():
        deferred.cancel()
    delayedTimeOutCall = reactor.callLater(seconds, timedOutCall)

    # If Deferred has result, cancel the timeout:
    def cancelTimeout(result):
        if delayedTimeOutCall.active():
            delayedTimeOutCall.cancel()
        return result
    deferred.addBoth(cancelTimeout)

    return delayedTimeOutCall


def getBranch(build):
    """
    Return the branch for a given build.

    If the build has more than one branch, return the first branch found. If a
    build has no branches, then raise a ValueError.

    :param build: An ``IBuildStatus``.
    :return: A branch as a string, e.g. 'master', 'some-feature-FLOC-1234'.
    """
    sourceStamps = build.getSourceStamps()
    try:
        return (stamp.branch for stamp in sourceStamps).next()
    except StopIteration:
        raise ValueError('{} has no source stamps'.format(build))
