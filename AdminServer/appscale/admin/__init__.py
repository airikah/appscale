""" A server that handles application deployments. """

import argparse
import datetime
import logging
import socket
import sys
import time
import uuid

from appscale.common import appscale_info
from appscale.common.constants import HTTPCodes
from appscale.common.constants import LOG_FORMAT
from appscale.common.ua_client import UAClient
from appscale.common.ua_client import UAException
from appscale.common.unpackaged import APPSCALE_PYTHON_APPSERVER
from tornado import gen
from tornado.options import options
from tornado import web
from tornado.escape import json_decode
from tornado.escape import json_encode
from tornado.ioloop import IOLoop
from . import utils
from . import constants
from .constants import (
  CustomHTTPError,
  MAX_DEPLOY_TIME,
  OperationTimeout,
  REDEPLOY_WAIT,
  VALID_RUNTIMES
)
from .operations_cache import OperationsCache

sys.path.append(APPSCALE_PYTHON_APPSERVER)
from google.appengine.api.appcontroller_client import AppControllerException


# The state of each operation.
operations = OperationsCache()


@gen.coroutine
def wait_for_port_assignment(operation_id, deadline, acc):
  """ Waits until port is assigned for version.

  Args:
    operation_id: A string specifying an operation ID.
    deadline: A float containing a unix timestamp.
    acc: An AppControllerClient.
  Raises:
    OperationTimeout if the deadline is exceeded.
  """
  operation = operations[operation_id]
  try:
    project_id = operation['project_id']
  except KeyError:
    raise OperationTimeout('Operation no longer in cache')

  while True:
    if time.time() > deadline:
      message = 'Deploy operation took too long.'
      operation['done'] = True
      operation['error'] = {'message': message}
      raise OperationTimeout(message)

    try:
      info_map = acc.get_app_info_map()
    except AppControllerException as error:
      logging.warning('Unable to fetch info map: {}'.format(error))
      yield gen.sleep(1)
      continue

    try:
      raise gen.Return(info_map[project_id]['nginx'])
    except KeyError:
      yield gen.sleep(1)
      continue


@gen.coroutine
def wait_for_port_to_open(http_port, operation_id, deadline):
  """ Waits until port is open.

  Args:
    http_port: An integer specifying the version's port number.
    operation_id: A string specifying an operation ID.
    deadline: A float containing a unix timestamp.
  Raises:
    OperationTimeout if the deadline is exceeded.
  """
  logging.debug('Waiting for {} to open'.format(http_port))
  try:
    operation = operations[operation_id]
  except KeyError:
    raise OperationTimeout('Operation no longer in cache')

  while True:
    if time.time() > deadline:
      message = 'Deploy operation took too long.'
      operation['done'] = True
      operation['error'] = {'message': message}
      raise OperationTimeout(message)

    sock = socket.socket()
    result = sock.connect_ex((options.login_ip, http_port))
    if result == 0:
      break

    yield gen.sleep(1)


@gen.coroutine
def wait_for_deploy(operation_id, acc):
  """ Tracks the progress of a deployment.

  Args:
    operation_id: A string specifying the operation ID.
    acc: An AppControllerClient instance.
  Raises:
    OperationTimeout if the deadline is exceeded.
  """
  try:
    operation = operations[operation_id]
  except KeyError:
    raise OperationTimeout('Operation no longer in cache')

  start_time = time.time()
  deadline = start_time + MAX_DEPLOY_TIME

  http_port = yield wait_for_port_assignment(operation_id, deadline, acc)
  yield wait_for_port_to_open(http_port, operation_id, deadline)

  operation['done'] = True
  create_time = datetime.datetime.utcnow()
  operation['response'] = utils.format_version(
    operation, constants.ServingStatus.SERVING, create_time, http_port)
  logging.info('Finished operation {}'.format(operation_id))


class BaseHandler(web.RequestHandler):
  """ A base handler. """
  def authenticate(self):
    """ Ensures requests are authenticated.

    Raises:
      CustomHTTPError if the secret is invalid.
    """
    if 'AppScale-Secret' not in self.request.headers:
      message = 'A required header is missing: AppScale-Secret'
      raise CustomHTTPError(HTTPCodes.UNAUTHORIZED, message=message)

    if self.request.headers['AppScale-Secret'] != options.secret:
      raise CustomHTTPError(HTTPCodes.UNAUTHORIZED, message='Invalid secret')

  def write_error(self, status_code, **kwargs):
    """ Writes a custom JSON-based error message.

    Args:
      status_code: An integer specifying the HTTP error code.
    """
    details = {'code': status_code}
    if 'exc_info' in kwargs:
      error = kwargs['exc_info'][1]
      try:
        details.update(error.kwargs)
      except AttributeError:
        pass

    self.finish(json_encode({'error': details}))


class VersionsHandler(BaseHandler):
  """ Manages service versions. """
  def initialize(self, acc, ua_client):
    """ Defines an AppControllerClient and UAClient.

    Args:
      acc: An AppControllerClient.
      ua_client: A UAClient.
    """
    self.acc = acc
    self.ua_client = ua_client

  def get_current_user(self):
    """ Retrieves the current user.

    Returns:
      A string specifying the user's email address.
    Raises:
      CustomHTTPError if the user is invalid.
    """
    if 'AppScale-User' not in self.request.headers:
      message = 'A required header is missing: AppScale-User'
      raise CustomHTTPError(HTTPCodes.BAD_REQUEST, message=message)

    user = self.request.headers['AppScale-User']
    try:
      user_exists = self.ua_client.does_user_exist(user)
    except UAException:
      message = 'Unable to determine if user exists: {}'.format(user)
      logging.exception(message)
      raise CustomHTTPError(HTTPCodes.INTERNAL_ERROR, message=message)

    if not user_exists:
      raise CustomHTTPError(HTTPCodes.BAD_REQUEST,
                            message='User does not exist: {}'.format(user))

    return user

  def version_from_payload(self):
    """ Constructs version from payload.
    
    Returns:
      A dictionary containing version details.
    Raises:
      CustomHTTPError if payload is invalid.
    """
    try:
      version = json_decode(self.request.body)
    except ValueError:
      raise CustomHTTPError(HTTPCodes.BAD_REQUEST,
                            message='Payload must be valid JSON')
    required_fields = ('deployment.zip.sourceUrl', 'id', 'runtime')
    utils.assert_fields_in_resource(required_fields, 'version', version)
    if version['runtime'] not in VALID_RUNTIMES:
      message = 'Invalid runtime: {}'.format(version['runtime'])
      raise CustomHTTPError(HTTPCodes.BAD_REQUEST, message=message)

    if version['runtime'] in [constants.JAVA, constants.PYTHON27]:
      utils.assert_fields_in_resource(['threadsafe'], 'version', version)

    if version['id'] != constants.DEFAULT_VERSION:
      raise CustomHTTPError(HTTPCodes.BAD_REQUEST,
                            message='Invalid version ID')

    return version

  def project_exists(self, project_id):
    """ Checks if a project exists.
    
    Args:
      project_id: A string specifying a project ID.
    Raises:
      CustomHTTPError if unable to determine if project exists.
    """
    try:
      return self.ua_client.does_app_exist(project_id)
    except UAException:
      message = 'Unable to check if project exists: {}'.format(project_id)
      logging.exception(message)
      raise CustomHTTPError(HTTPCodes.INTERNAL_ERROR, message=message)

  def create_project(self, project_id, user, runtime):
    """ Creates a new project.
    
    Args:
      project_id: A string specifying a project ID.
      user: A string specifying a user's email address.
      runtime: A string specifying the project's runtime.
    Raises:
      CustomHTTPError if unable to create new project.
    """
    logging.info('Creating project: {}'.format(project_id))
    try:
      self.ua_client.commit_new_app(project_id, user, runtime)
    except UAException:
      message = 'Unable to ensure project exists: {}'.format(project_id)
      logging.exception(message)
      raise CustomHTTPError(HTTPCodes.INTERNAL_ERROR, message=message)

  def ensure_user_is_owner(self, project_id, user):
    """ Ensures a user is the owner of a project.
    
    Args:
      project_id: A string specifying a project ID.
      user: A string specifying a user's email address.
    Raises:
      CustomHTTPError if the user is not the owner.
    """
    try:
      project_metadata = self.ua_client.get_app_data(project_id)
    except UAException:
      message = 'Unable to retrieve project metadata'
      logging.exception(message)
      raise CustomHTTPError(HTTPCodes.INTERNAL_ERROR, message=message)

    if 'owner' not in project_metadata:
      raise CustomHTTPError(HTTPCodes.INTERNAL_ERROR,
                            message='Project owner not defined')

    if project_metadata['owner'] != user:
      message = 'User is not project owner: {}'.format(user)
      raise CustomHTTPError(HTTPCodes.FORBIDDEN, message=message)

  def begin_deploy(self, project_id, source_path):
    """ Triggers the deployment process.
    
    Args:
      project_id: A string specifying a project ID.
      source_path: A string specifying the location of the version's source.
    Raises:
      CustomHTTPError if unable to start the deployment process.
    """
    try:
      self.acc.done_uploading(project_id, source_path)
    except AppControllerException as error:
      message = 'Error while setting sourceUrl: {}'.format(error)
      raise CustomHTTPError(HTTPCodes.INTERNAL_ERROR, message=message)

    try:
      self.acc.update([project_id])
    except AppControllerException as error:
      message = 'Error while updating application: {}'.format(error)
      raise CustomHTTPError(HTTPCodes.INTERNAL_ERROR, message=message)

  def post(self, project_id, service_id):
    """ Creates or updates a version.
    
    Args:
      project_id: A string specifying a project ID.
      service_id: A string specifying a service ID.
    """
    self.authenticate()
    user = self.get_current_user()
    version = self.version_from_payload()
    project_exists = self.project_exists(project_id)
    if not project_exists:
      self.create_project(project_id, user, version['runtime'])

    if service_id != constants.DEFAULT_SERVICE:
      raise CustomHTTPError(HTTPCodes.BAD_REQUEST, message='Invalid service')

    self.ensure_user_is_owner(project_id, user)

    # Strip protocol prefix from sourceUrl.
    proto_prefix = 'file://'
    source_path = version['deployment']['zip']['sourceUrl']
    if source_path.startswith(proto_prefix):
      source_path = source_path[len(proto_prefix):]

    self.begin_deploy(project_id, source_path)

    operation_id = str(uuid.uuid4())
    insert_time = datetime.datetime.utcnow()

    operations[operation_id] = {
      'id': operation_id,
      'project_id': project_id,
      'method': constants.Methods.CREATE_VERSION,
      'start_time': insert_time,
      'service_id': service_id,
      'version_id': version['id'],
      'runtime': version['runtime'],
      'source': version['deployment']['zip']['sourceUrl'],
      'done': False
    }
    if 'threadsafe' in version:
      operations[operation_id]['threadsafe'] = version['threadsafe']

    pre_wait = REDEPLOY_WAIT if project_exists else 0
    logging.debug(
      'Starting operation {} in {}s'.format(operation_id, pre_wait))
    IOLoop.current().call_later(pre_wait, wait_for_deploy, operation_id,
                                self.acc)

    output = utils.format_operation(operations[operation_id])
    self.write(json_encode(output))


class OperationsHandler(BaseHandler):
  """ Retrieves operations. """
  def get(self, project_id, operation_id):
    """ Retrieves operation status.
    
    Args:
      project_id: A string specifying a project ID.
      operation_id: A string specifying an operation ID.
    """
    self.authenticate()
    if operation_id not in operations:
      raise CustomHTTPError(HTTPCodes.NOT_FOUND,
                            message='Operation not found.')

    output = utils.format_operation(operations[operation_id])
    self.write(json_encode(output))


def main():
  """ Starts the AdminServer. """
  logging.basicConfig(format=LOG_FORMAT, level=logging.INFO)

  parser = argparse.ArgumentParser()
  parser.add_argument('-p', '--port', type=int, default=constants.DEFAULT_PORT,
                      help='The port to listen on')
  parser.add_argument('-v', '--verbose', action='store_true',
                      help='Output debug-level logging')
  args = parser.parse_args()

  if args.verbose:
    logging.getLogger().setLevel(logging.DEBUG)

  options.define('secret', appscale_info.get_secret())
  options.define('login_ip', appscale_info.get_login_ip())

  acc = appscale_info.get_appcontroller_client()
  ua_client = UAClient(appscale_info.get_db_master_ip(), options.secret)

  app = web.Application([
    ('/v1/apps/([a-z0-9-]+)/services/([a-z0-9-]+)/versions', VersionsHandler,
     {'acc': acc, 'ua_client': ua_client}),
    ('/v1/apps/([a-z0-9-]+)/operations/([a-z0-9-]+)', OperationsHandler),
  ])
  logging.info('Starting AdminServer')
  app.listen(args.port)
  io_loop = IOLoop.current()
  io_loop.start()
