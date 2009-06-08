""" 
    collect and run test items. 

    * executing test items 
    * running collectors 
    * and generating report events about it 
"""

import py

from py.__.test.outcome import Skipped

#
# pytest plugin hooks 
#

def pytest_addoption(parser):
    group = parser.getgroup("general") 
    group.addoption('--boxed',
               action="store_true", dest="boxed", default=False,
               help="box each test run in a separate process") 

def pytest_configure(config):
    config._setupstate = SetupState()

def pytest_unconfigure(config):
    config._setupstate.teardown_all()

def pytest_make_collect_report(collector):
    call = collector.config.guardedcall(
        lambda: collector._memocollect()
    )
    result = None
    if not call.excinfo:
        result = call.result
    return CollectReport(collector, result, call.excinfo, call.outerr)

    return report 

def pytest_runtest_protocol(item):
    if item.config.option.boxed:
        report = forked_run_report(item)
    else:
        report = basic_run_report(item) 
    item.config.hook.pytest_runtest_logreport(rep=report)
    return True

def pytest_runtest_setup(item):
    item.config._setupstate.prepare(item)

def pytest_runtest_call(item):
    if not item._deprecated_testexecution():
        item.runtest()

def pytest_runtest_makereport(item, excinfo, when, outerr):
    return ItemTestReport(item, excinfo, when, outerr)

def pytest_runtest_teardown(item):
    item.config._setupstate.teardown_exact(item)

#
# Implementation
#

class Call:
    excinfo = None 
    def __init__(self, when, func):
        self.when = when 
        try:
            self.result = func()
        except KeyboardInterrupt:
            raise
        except:
            self.excinfo = py.code.ExceptionInfo()


def basic_run_report(item):
    """ return report about setting up and running a test item. """
    capture = item.config._getcapture()
    hook = item.config.hook
    try:
        call = Call("setup", lambda: hook.pytest_runtest_setup(item=item))
        if not call.excinfo:
            call = Call("call", lambda: hook.pytest_runtest_call(item=item))
            # in case of an error we defer teardown to not shadow the error
            if not call.excinfo:
                call = Call("teardown", lambda: hook.pytest_runtest_teardown(item=item))
    finally:
        outerr = capture.reset()
    return item.config.hook.pytest_runtest_makereport(
            item=item, excinfo=call.excinfo, 
            when=call.when, outerr=outerr)

def forked_run_report(item):
    EXITSTATUS_TESTEXIT = 4
    from py.__.test.dist.mypickle import ImmutablePickler
    ipickle = ImmutablePickler(uneven=0)
    ipickle.selfmemoize(item.config)
    def runforked():
        try:
            testrep = basic_run_report(item)
        except KeyboardInterrupt: 
            py.std.os._exit(EXITSTATUS_TESTEXIT)
        return ipickle.dumps(testrep)

    ff = py.process.ForkedFunc(runforked)
    result = ff.waitfinish()
    if result.retval is not None:
        return ipickle.loads(result.retval)
    else:
        if result.exitstatus == EXITSTATUS_TESTEXIT:
            py.test.exit("forked test item %s raised Exit" %(item,))
        return report_process_crash(item, result)

def report_process_crash(item, result):
    path, lineno = item._getfslineno()
    longrepr = [
        ("X", "CRASHED"), 
        ("%s:%s: CRASHED with signal %d" %(path, lineno, result.signal)),
    ]
    return ItemTestReport(item, excinfo=longrepr, when="???")

class BaseReport(object):
    def __repr__(self):
        l = ["%s=%s" %(key, value)
           for key, value in self.__dict__.items()]
        return "<%s %s>" %(self.__class__.__name__, " ".join(l),)

    def toterminal(self, out):
        longrepr = self.longrepr 
        if hasattr(longrepr, 'toterminal'):
            longrepr.toterminal(out)
        else:
            out.line(str(longrepr))
   
class ItemTestReport(BaseReport):
    failed = passed = skipped = False

    def __init__(self, item, excinfo=None, when=None, outerr=None):
        self.item = item 
        if item and when != "setup":
            self.keywords = item.readkeywords() 
        else:
            # if we fail during setup it might mean 
            # we are not able to access the underlying object
            # this might e.g. happen if we are unpickled 
            # and our parent collector did not collect us 
            # (because it e.g. skipped for platform reasons)
            self.keywords = {}  
        if not excinfo:
            self.passed = True
            self.shortrepr = "." 
        else:
            self.when = when 
            if not isinstance(excinfo, py.code.ExceptionInfo):
                self.failed = True
                shortrepr = "?"
                longrepr = excinfo 
            elif excinfo.errisinstance(Skipped):
                self.skipped = True 
                shortrepr = "s"
                longrepr = self.item._repr_failure_py(excinfo, outerr)
            else:
                self.failed = True
                shortrepr = self.item.shortfailurerepr
                if self.when == "call":
                    longrepr = self.item.repr_failure(excinfo, outerr)
                else: # exception in setup or teardown 
                    longrepr = self.item._repr_failure_py(excinfo, outerr)
                    shortrepr = shortrepr.lower()
            self.shortrepr = shortrepr 
            self.longrepr = longrepr 

    def getnode(self):
        return self.item 

class CollectReport(BaseReport):
    skipped = failed = passed = False 

    def __init__(self, collector, result, excinfo=None, outerr=None):
        self.collector = collector 
        if not excinfo:
            self.passed = True
            self.result = result 
        else:
            self.outerr = outerr
            self.longrepr = self.collector._repr_failure_py(excinfo, outerr)
            if excinfo.errisinstance(Skipped):
                self.skipped = True
                self.reason = str(excinfo.value)
            else:
                self.failed = True

    def getnode(self):
        return self.collector 

class SetupState(object):
    """ shared state for setting up/tearing down test items or collectors. """
    def __init__(self):
        self.stack = []
        self._finalizers = {}

    def addfinalizer(self, finalizer, colitem):
        """ attach a finalizer to the given colitem. 
        if colitem is None, this will add a finalizer that 
        is called at the end of teardown_all(). 
        """
        assert callable(finalizer)
        #assert colitem in self.stack
        self._finalizers.setdefault(colitem, []).append(finalizer)

    def _pop_and_teardown(self):
        colitem = self.stack.pop()
        self._teardown_with_finalization(colitem)

    def _teardown_with_finalization(self, colitem): 
        finalizers = self._finalizers.pop(colitem, None)
        while finalizers:
            fin = finalizers.pop()
            fin()
        if colitem: 
            colitem.teardown()
        for colitem in self._finalizers:
            assert colitem is None or colitem in self.stack

    def teardown_all(self): 
        while self.stack: 
            self._pop_and_teardown()
        self._teardown_with_finalization(None)
        assert not self._finalizers

    def teardown_exact(self, item):
        assert self.stack and self.stack[-1] == item
        self._pop_and_teardown()
     
    def prepare(self, colitem): 
        """ setup objects along the collector chain to the test-method
            Teardown any unneccessary previously setup objects."""
        needed_collectors = colitem.listchain() 
        while self.stack: 
            if self.stack == needed_collectors[:len(self.stack)]: 
                break 
            self._pop_and_teardown()
        for col in needed_collectors[len(self.stack):]: 
            col.setup() 
            self.stack.append(col) 