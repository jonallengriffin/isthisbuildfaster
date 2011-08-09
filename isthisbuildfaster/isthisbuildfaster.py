from BeautifulSoup import BeautifulSoup
from collections import defaultdict
import datetime
import gzip
from io import BytesIO
import json
import optparse
import os
import re
import sys
import urllib2

from mozautoeslib import ESLib
from mozautolog import ESAutologTestGroup as AutologTestGroup

DEBUG = False

eslib = ESLib('elasticsearch1.metrics.sjc1.mozilla.com:9200', 'logs', 'testruns')
tryurl = 'http://stage.mozilla.org/pub/mozilla.org/firefox/try-builds/'

class LogFile(object):
  # finds the command-line used to start a test run
  mochitestRe = re.compile(r"^python mochitest.*")
  reftestRe = re.compile(r"^python reftest/runreftest.py.*")
  xpcshellRe = re.compile(r"^bash -c.*?-u xpcshell/runxpcshelltests.py")
  chunkRe = re.compile(r'--this-chunk (\d)')
  elapsedTimeRe = re.compile(r"elapsedTime=(\d+)")

  def __init__(self, filename, baseurl, tree='try', os=None,
               platform=None, testgroup=None, debug=None):
    self.filename = filename
    self.baseurl = baseurl
    self.tree = tree
    self.os = os
    self.buildtype = 'debug' if debug else 'opt'
    self.testgroup = testgroup
    self.platform = platform
    self.suites = []

  def _download(self):
    # download the log file and extract it...
    url = os.path.join(self.baseurl, self.filename).replace('\\', '/')
    remote = urllib2.urlopen(url)
    gzfile = gzip.GzipFile(fileobj=BytesIO(remote.read()))
    return gzfile

  def parse(self):
    f = self._download()
    line = f.readline()
    insuite = None
    while line:
      if insuite:
        m = self.elapsedTimeRe.match(line)
        if m:
          self.suites.append((insuite, m.group(1)))
          insuite = None
      elif self.mochitestRe.match(line):
        m = self.chunkRe.search(line)
        if m:
          insuite = 'mochitest-%s' % m.group(1)
        elif 'browser-chrome' in line:
          insuite = 'mochitest-browser-chrome'
        elif 'chrome' in line:
          insuite = 'mochitest-chrome'
        elif '--a11y' in line:
          insuite = 'mochitest-a11y'
        elif '--ipcplugins' in line:
          insuite = 'mochitest-ipcplugins'
      elif self.reftestRe.match(line):
        if 'crashtest' in line:
          insuite = 'crashtest'
        elif 'jsreftest' in line:
          insuite = 'jsreftest'
        else:
          insuite = 'reftest'
      elif self.xpcshellRe.match(line):
        insuite = 'xpcshell'
      line = f.readline()


def get_links(doc):
  '''Return a list of links in a document that have identical contents
     and href attributes.
  '''
  links = []
  soup = BeautifulSoup(doc)
  ahrefs = soup.findAll('a')
  for link in ahrefs:
    if link.string == link['href']:
       links.append(link.string)
  return links

def find_most_recent_completed_commit():
  '''Find the most recent commit which has a full set of test results in
     ES.
  '''
  endday = datetime.datetime.today()
  startday = endday - datetime.timedelta(days=7)
  result = eslib.frequency(include={'tree': 'mozilla-central',
                                    'date': [startday, endday]},
                                    frequency_fields=['revision']
                          )
  commit = { 'revision': None, 'buildid': None }
  for rev in result['revision']['terms']:
    if rev['count'] > 140:
      result = eslib.query({'tree': 'mozilla-central',
                            'revision': rev['term']},
                           size=1)
      if result:
        buildid = result[0]['buildid']
        if not commit['buildid'] or buildid > commit['buildid']:
          commit = { 'revision': result[0]['revision'], 'buildid': buildid }

  return None if not commit['revision'] else commit['revision']

def get_durations_for_ES_commit(revision):
  '''Return a dict of all the test durations for a particular commit in ES.
  '''
  mochiRe = re.compile(r'mochitests-(\d)')
  results = eslib.query({'tree': 'mozilla-central',
                         'revision': revision})
  control = defaultdict(lambda: defaultdict(lambda: defaultdict))
  for result in results:
    plat = result['platform'] if result['platform'] != "win32" else result['os']
    testsuite = result['testsuite'] if result['testsuite'] != 'mochitest' else result['testgroup']
    m = mochiRe.match(testsuite)
    if m:
      testsuite = 'mochitest-%s' % m.group(1)
    control['%s-%s' % (plat, result['buildtype'])][testsuite] = result['elapsedtime']
  return control

def get_list_of_try_logs(folder):
  logs = []
  tree = 'try'
  url = "%s%s/" % (tryurl, folder)
  f = urllib2.urlopen(url)
  doc = f.read()
  folders = get_links(doc)
  for folder in folders:
    folderurl = url + folder
    f = urllib2.urlopen(folderurl)
    doc = f.read()
    files = get_links(doc)
    for file in files:
      logs.append('%s%s' % (folderurl, file))

  return logs

def get_durations_from_trylogs(trylogs):
  results = defaultdict(lambda: defaultdict(lambda: defaultdict))
  testfileRe = re.compile(r'(try)[-|_](.*?)(-debug|-o-debug)?[-|_](test|unittest)-(.*?)-build')

  for trylog in trylogs:
    m = testfileRe.match(os.path.basename(trylog))
    if m:
      _os = m.group(2)
      debug = True if m.group(3) else False
      testgroup = m.group(5)
      platform = AutologTestGroup.get_platform_from_os(_os)
      if DEBUG:
        print 'parsing', os.path.basename(trylog)
      logfile = LogFile(os.path.basename(trylog), os.path.dirname(trylog), os=_os, platform=platform, debug=debug, testgroup=testgroup)
      if platform != 'android':
        logfile.parse()
        for suite in logfile.suites:
          plat = logfile.platform if logfile.platform != "win32" else logfile.os
          results['%s-%s' % (plat, logfile.buildtype)][suite[0]] = suite[1]

  return results

def compare_test_durations(tree1, revision1, tree2, revision2, submitter):
  if DEBUG:
    print 'finding the most recent changeset with all tests completed'
  control_revision = find_most_recent_completed_commit()
  if not control_revision:
    return None

  if DEBUG:
    print 'getting durations from ES for changeset', control_revision
  control = get_durations_for_ES_commit(control_revision)

  trylogs = get_list_of_try_logs('%s-%s' % (submitter, revision2))

  if DEBUG:
    print "parsing try logs"
  test = get_durations_from_trylogs(trylogs)

  total_diff = 0
  results = defaultdict(lambda: defaultdict(list))
  for plat in control:
    after = test.get(plat, {})
    before = control.get(plat, {})
    for suite in before:
      before_duration = int(before.get(suite))
      after_duration = int(after.get(suite, -1))
      difference = after_duration - before_duration if after_duration > -1 else 0
      total_diff += difference
      results[plat][suite] = [before_duration, after_duration, difference]

  return { 'durations': results,
           'difference': total_diff,
           'revisions': [
            { 'tree': 'mozilla-central',
              'revision': control_revision },
            { 'tree': 'try',
              'revision': revision2 }
           ]}

def main():
  parser = optparse.OptionParser()
  parser.add_option("--tree",
                    action = "store", type = "string", dest = "tree",
                    default = 'try',
                    help = "tree of revision to compare against")
  parser.add_option("--rev",
                    action = "store", type = "string", dest = "revision",
                    help = "revision to compare against")
  parser.add_option("--submitter",
                    action = "store", type = "string", dest = "submitter",
                    help = "submitter of revision to compare against")
  (options, args) = parser.parse_args()

  res = compare_test_durations('mozilla-central', None,
                               options.tree, options.revision, options.submitter)
  print json.dumps(res, indent=2)

if __name__ == "__main__":
  main()
