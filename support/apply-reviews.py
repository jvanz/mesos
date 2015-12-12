#!/usr/bin/env python
import argparse
import atexit
import json
import linecache
import os
import re
import subprocess
import sys
import urllib2


REVIEWBOARD_REVIEW_URL = 'https://reviews.apache.org/r'
REVIEWBOARD_API_URL =\
  'https://reviews.apache.org/api/review-requests'
REVIEWBOARD_USER_URL = 'https://reviews.apache.org/api/users'


GITHUB_URL = 'https://api.github.com/repos/apache/mesos/pulls'
GITHUB_PATCH_URL =\
  'https://patch-diff.githubusercontent.com/raw/apache/mesos/pull'


def review_api_url(review_id):
  """Returns a Review Board API URL given a review ID."""
  # Reviewboard REST API expects '/' at the end of the URL.
  return '{base}/{review}/'.format(base=REVIEWBOARD_API_URL, review=review_id)


def review_url(review_id):
  """Returns a Review Board UI URL given a review ID."""
  return '{base}/{review}/'.format(base=REVIEWBOARD_REVIEW_URL, review=review_id)


def pull_request_url(pull_request_number):
  """Returns a GitHub pull request URL given a PR number."""
  return '{base}/{pr}'.format(base=GITHUB_URL, pr=pull_request_number)


def reviewboard_user_url(username):
  """Returns a Review Board URL for a user given a username."""
  # Reviewboard REST API expects '/' at the end of the URL.
  return '{base}/{user}/'.format(base=REVIEWBOARD_USER_URL, user=username)


def patch_url():
  """Returns a Review Board or a GitHub URL for a patch."""
  if options['review_id']:
    # Reviewboard REST API expects '/' at the end of the URL.
    return '{base}/{review}/diff/raw/'.format(base=REVIEWBOARD_REVIEW_URL,
                                              review=options['review_id'])
  elif options['github']:
    return '{base}/{patch}.patch'.format(base=GITHUB_PATCH_URL,
                                         patch=options['github'])
  return None


def url_to_json(url):
  """Performs HTTP request and returns JSON-ified response."""
  json_str = urllib2.urlopen(url)
  return json.loads(json_str.read())


def extract_review_id(url):
  """Extracts review ID from Review Board URL."""
  review_id = re.search(REVIEWBOARD_API_URL + '/(\d+)/', url)
  if review_id:
    return review_id.group(1)


def review_chain(review_id):
  """Returns a parent review chain for a given review ID."""
  json_obj = url_to_json(review_api_url(review_id))

  # Stop as soon as we stumble upon a submitted request.
  status = json_obj.get('review_request').get('status')
  if status == "submitted":
    return []

  # Verify that the review has exactly one parent.
  parent = json_obj.get('review_request').get('depends_on')
  if len(parent) > 1:
    sys.stderr.write(
      'Error: Review {review} has more than one parent'\
      .format(review=review_id))
    sys.exit(1)
  elif len(parent) == 0:
    return [(review_id, json_obj.get('review_request').get('summary'))]
  else:
    # The review has exactly one parent.
    review_list = review_chain(extract_review_id(parent[0].get('href')))

    review = (review_id, json_obj.get('review_request').get('summary'))
    if review not in review_list:
      return review_list + [review]
    else:
      sys.stderr.write('Found a circular dependency in the chain starting at '
                       '{review}\n'.format(review=review_id))
      sys.exit(1)


def shell(command, dry_run):
  """Runs a command in a shell, unless the dry-run option is set (in which
  case it just prints the command."""
  if dry_run:
    print command
    return

  error_code = subprocess.call(command, stderr=subprocess.STDOUT, shell=True)
  if error_code != 0:
    sys.exit(error_code)


def remove_patch(file_name=None):
  """Removes the file. In case the file name is not provided it reads the
  file name from global options dictionary."""
  if file_name == None:
    cmd = 'rm -f {_file}.patch'.format(_file=patch_id())
  else:
    cmd = 'rm -f {_file}'.format(_file=file_name)

  # In case of github we always need to fetch the patch to extract username and
  # email, so to ensure that it always gets cleaned up we ignore the dry_run
  # option by always setting the second parameter to False.
  if options['github']:
    shell(cmd, False)
  else:
    shell(cmd, options['dry_run'])


def apply_review():
  """Applies a review with a given ID locally."""
  # Make sure we don't leave the patch behind in case of failure.
  atexit.register(lambda: remove_patch('{_file}.patch'\
                                       .format(_file=patch_id())))

  fetch_patch()
  apply_patch()
  commit_patch()
  remove_patch()


def fetch_patch():
  """Fetches a patch from Review Board or GitHub."""
  cmd = ' '.join(['wget',
                 '--no-check-certificate',
                 '--no-verbose',
                 '-O '
                 '{review_id}.patch',
                 '{url}'])\
                 .format(review_id=patch_id(), url=patch_url())

  # In case of github we always need to fetch the patch to extract username
  # and email, so we ignore the dry_run option by setting the second parameter
  # to False.
  if options['github']:
    shell(cmd, False)
  else:
    shell(cmd, options['dry_run'])


def patch_id():
  return (options['review_id'] or options['github'])


def apply_patch():
  """Applies patch locally."""
  cmd = 'git apply --index {review_id}.patch'\
        .format(review_id=patch_id())
  shell(cmd, options['dry_run'])


def quote(string):
  """Quote a variable so it can be safely used in shell."""
  return string.replace("'", "'\\''")


def commit_patch():
  """Commits patch locally."""
  data = patch_data()

  # Check whether we need to amend the commit message.
  if options['no_amend']:
    amend = ''
  else:
    amend = '-e'

  cmd = 'git commit --author \'{author}\' {_amend} -am \'{message}\''\
        .format(author=quote(data['author']),
                _amend=amend,
                message=quote(data['message']))
  shell(cmd, options['dry_run'])


def patch_data():
  """Populates and returns a dictionary with data necessary for committing the
  patch (such as the message, the author, etc.)."""
  if options['review_id']:
    return reviewboard_data()
  elif options['github']:
    return github_data()
  else:
    return None


def get_author(patch):
  """Reads the author name and email from the .patch file"""
  author = linecache.getline(patch, 2)
  return author.replace('From: ', '').rstrip()


def github_data():
  pull_request_number = options['github']
  pull_request = url_to_json(pull_request_url(pull_request_number))

  title = pull_request.get('title')
  description = pull_request.get('body')
  url = '{url}/{pr}'.format(url=GITHUB_URL, pr=pull_request_number)
  author = get_author('{pr}.patch'.format(pr=pull_request_number))
  message = '\n\n'.join(['{summary}',
                         '{description}',
                         'This closes: {pr}'])\
                         .format(summary=title,
                                 description=description,
                                 pr=pull_request_number)

  review_data = {
    "summary": title,
    "description": description,
    "url": url,
    "author": author,
    "message": message
  }

  return review_data


def reviewboard_data():
  """Fetches review data and populates internal data structure."""
  review_id = options['review_id']

  # Populate review object.
  review = url_to_json(review_api_url(review_id)).get('review_request')

  url = review_url(review_id)

  # Populate user object.
  user = url_to_json(reviewboard_user_url(
    review.get('links').get('submitter').get('title'))).get('user')

  author = '{author} <{email}>'.format(author=user.get('fullname'),
                                       email=user.get('email'))
  message = '\n\n'.join(['{summary}',
                          '{description}',
                          'Review: {review_url}'])\
                          .format(summary=review.get('summary'),
                                  description=review.get('description'),
                                  review_url=url)

  review_data = {
    "summary": review.get('summary'),
    "description": review.get('description'),
    "url": url,
    "author": author,
    "message": message
  }

  return review_data


# A global dictionary for holding execution options.
options = {}


def parse_options():
  """Parses command line options and populates the dictionary."""
  parser = argparse.ArgumentParser(
    description = 'Recursively apply Review Board reviews'
                  ' and GitHub pull requests.')

  parser.add_argument('-d',
                      '--dry-run',
                      action='store_true',
                      help='Perform a dry run.')
  parser.add_argument('-n', '--no-amend',
                      action='store_true',
                      help='Do not amend commit message.')

  # Add -g and -r and make them mutually exclusive.
  group = parser.add_mutually_exclusive_group(required=True)
  group.add_argument('-g', '--github',
                     metavar='PULL_REQUEST',
                     help='Pull request number')
  group.add_argument('-r', '--review-id',
                      metavar='REVIEW_ID',
                      help='Numeric Review ID')

  args = parser.parse_args()

  options['review_id'] = args.review_id
  options['dry_run'] = args.dry_run
  options['no_amend'] = args.no_amend
  options['github'] = args.github


def reviewboard():
  """Applies a chain of reviewboard patches."""
  # Retrieve the list of reviews to apply.
  reviews = review_chain(options['review_id'])

  applied = set()
  for review_id, summary in reviews:
    if review_id not in applied:
      applied.add(review_id)
      options['review_id'] = review_id
      apply_review()


def github():
  """Applies a patch from github."""
  apply_review()


# A global dictionary for holding command line options. See parse_options()
# function for details.
#
# TODO(hartem): Consider getting rid of global options variable. Either use
# explicit arguments or classes.
options = {}


if __name__ == "__main__":
  # Parse command line options and populate the 'options' dictionary.
  parse_options()

  if options['review_id']:
    reviewboard()
  else:
    github()
