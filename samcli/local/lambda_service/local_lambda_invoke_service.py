"""Local Lambda Service that only invokes a function"""

import json
import logging
import io
import threading

from uuid import uuid4
from flask import Flask, request

from samcli.lib.utils.stream_writer import StreamWriter
from samcli.local.services.base_local_service import BaseLocalService, LambdaOutputParser, CaseInsensitiveDict
from samcli.local.lambdafn.exceptions import FunctionNotFound
from .lambda_error_responses import LambdaErrorResponses

LOG = logging.getLogger(__name__)


class LocalLambdaInvokeService(BaseLocalService):

    def __init__(self, lambda_runner, port, host, stderr=None):
        """
        Creates a Local Lambda Service that will only response to invoking a function

        Parameters
        ----------
        lambda_runner samcli.commands.local.lib.local_lambda.LocalLambdaRunner
            The Lambda runner class capable of invoking the function
        port int
            Optional. port for the service to start listening on
        host str
            Optional. host to start the service on
        stderr io.BaseIO
            Optional stream where the stderr from Docker container should be written to
        """
        super(LocalLambdaInvokeService, self).__init__(lambda_runner.is_debugging(), port=port, host=host)
        self.lambda_runner = lambda_runner
        self.stderr = stderr

    def create(self):
        """
        Creates a Flask Application that can be started.
        """
        self._app = Flask(__name__)

        path = '/2015-03-31/functions/<function_name>/invocations'
        self._app.add_url_rule(path,
                               endpoint=path,
                               view_func=self._invoke_request_handler,
                               methods=['POST'],
                               provide_automatic_options=False)

        # setup request validation before Flask calls the view_func
        self._app.before_request(LocalLambdaInvokeService.validate_request)

        self._construct_error_handling()

    @staticmethod
    def validate_request():
        """
        Validates the incoming request

        The following are invalid
            1. The Request data is not json serializable
            2. Query Parameters are sent to the endpoint
            3. The Request Content-Type is not application/json
            4. 'X-Amz-Log-Type' header is not 'None'
            5. 'X-Amz-Invocation-Type' header is not 'RequestResponse'

        Returns
        -------
        flask.Response
            If the request is not valid a flask Response is returned

        None:
            If the request passes all validation
        """
        flask_request = request
        request_data = flask_request.get_data()

        if not request_data:
            request_data = b'{}'

        request_data = request_data.decode('utf-8')

        try:
            json.loads(request_data)
        except ValueError as json_error:
            LOG.debug("Request body was not json. Exception: %s", str(json_error))
            return LambdaErrorResponses.invalid_request_content(
                "Could not parse request body into json: No JSON object could be decoded")

        if flask_request.args:
            LOG.debug("Query parameters are in the request but not supported")
            return LambdaErrorResponses.invalid_request_content("Query Parameters are not supported")

        request_headers = CaseInsensitiveDict(flask_request.headers)

        log_type = request_headers.get('X-Amz-Log-Type', 'None')
        if log_type != 'None':
            LOG.debug("log-type: %s is not supported. None is only supported.", log_type)
            return LambdaErrorResponses.not_implemented_locally(
                "log-type: {} is not supported. None is only supported.".format(log_type))

        invocation_type = request_headers.get('X-Amz-Invocation-Type', 'RequestResponse')
        if invocation_type not in ['Event', 'RequestResponse']:
            LOG.warning("invocation-type: %s is not supported. Supported types: Event, RequestResponse.",
                        invocation_type)
            return LambdaErrorResponses.not_implemented_locally(
                ("invocation-type: {} is not supported. " +
                 "Supported types: Event, RequestResponse.").format(invocation_type))

    def _construct_error_handling(self):
        """
        Updates the Flask app with Error Handlers for different Error Codes

        """
        self._app.register_error_handler(500, LambdaErrorResponses.generic_service_exception)
        self._app.register_error_handler(404, LambdaErrorResponses.generic_path_not_found)
        self._app.register_error_handler(405, LambdaErrorResponses.generic_method_not_allowed)

    def _invoke_async_request(self, function_name, request_data, stdout, stderr):
        """
        Asynchronous Request Handler for the Local Lambda Invoke path. Flask response is returned immediately while the
        lambda function is run in a separate thread.

        Parameters
        ----------
        function_name str
            Name of function to invoke

        request_data str
            Parameters to pass to lambda function

        stdout io.BaseIO
            Output stream that stdout should be written to

        stderr io.BaseIO
            Output stream that stderr should be written to

        Returns
        -------
        A flask Response response object as if it was returned from Lambda
        """
        request_id = uuid4()

        thread = threading.Thread(target=self._invoke_sync_request,
                                  args=(function_name, request_data),
                                  kwargs={'stdout': stdout, 'stderr': stderr})
        thread.daemon = True
        thread.start()

        LOG.debug('Async invocation: %s, requestId=%s', function_name, request_id)
        return self.service_response(None,
                                     {'x-amzn-requestid': request_id},
                                     202)

    def _invoke_sync_request(self, function_name, request_data, stdout, stderr):
        """
        Synchronous Request Handler for the Local Lambda Invoke path.

        stdout_stream = io.BytesIO()
        stdout_stream_writer = StreamWriter(stdout_stream, self.is_debugging)

        try:
            self.lambda_runner.invoke(function_name, request_data, stdout=stdout_stream_writer, stderr=self.stderr)
        Parameters
        ----------
        function_name str
            Name of function to invoke

        request_data str
            Parameters to pass to lambda function

        stdout io.BaseIO
            Output stream that stdout should be written to

        stderr io.BaseIO
            Output stream that stderr should be written to

        Returns
        -------
        A flask Response response object as if it was returned from Lambda
        """

        try:
            self.lambda_runner.invoke(function_name, request_data, stdout=stdout, stderr=stderr)
        except FunctionNotFound:
            LOG.debug('%s was not found to invoke.', function_name)
            return LambdaErrorResponses.resource_not_found(function_name)

        lambda_response, lambda_logs, is_lambda_user_error_response = \
            LambdaOutputParser.get_lambda_output(stdout)

        if stderr and lambda_logs:
            # Write the logs to stderr if available.
            stderr.write(lambda_logs)

        if is_lambda_user_error_response:
            LOG.debug('Lambda error response: %s', lambda_response)
            return self.service_response(lambda_response,
                                         {'Content-Type': 'application/json', 'x-amz-function-error': 'Unhandled'},
                                         200)

        LOG.debug('Lambda returned success: %s', lambda_response)
        return self.service_response(lambda_response, {'Content-Type': 'application/json'}, 200)

    def _invoke_request_handler(self, function_name):
        """
        Determines what type of reuest handler should be used to invoke the Local Lambda.

        Parameters
        ----------
        function_name str
            Name of the function to invoke

        Returns
        -------
        A Flask Response response object as if it was returned from Lambda
        """
        flask_request = request

        request_data = flask_request.get_data()
        invocation_type = flask_request.headers.get('X-Amz-Invocation-Type', 'RequestResponse')

        if not request_data:
            request_data = b'{}'

        request_data = request_data.decode('utf-8')

        stdout_stream = io.BytesIO()

        if invocation_type == 'Event':
            return self._invoke_async_request(function_name, request_data, stdout_stream, self.stderr)

        return self._invoke_sync_request(function_name, request_data, stdout_stream, self.stderr)
