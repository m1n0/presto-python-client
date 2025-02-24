# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""

This module implements the Presto protocol to submit SQL statements, track
their state and retrieve their result as described in
https://github.com/prestodb/presto/wiki/HTTP-Protocol
and Presto source code.

The outline of a query is:
- Send HTTP POST to the coordinator
- Retrieve HTTP response with ``nextUri``
- Get status of the query execution by sending a HTTP GET to the coordinator

Presto queries are managed by the ``PrestoQuery`` class. HTTP requests are
managed by the ``PrestoRequest`` class. the status of a query is represented
by ``PrestoStatus`` and the result by ``PrestoResult``.


The main interface is :class:`PrestoQuery`: ::

    >> request = PrestoRequest(host='coordinator', port=8080, user='test')
    >> query =  PrestoQuery(request, sql)
    >> rows = list(query.execute())
"""
from __future__ import absolute_import, division, print_function

import logging
import os
from typing import Any, Dict, List, Optional, Text, Tuple, Union  # NOQA for mypy types
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, Union
import pytz

import prestodb.redirect
import requests
import six.moves.urllib_parse as parse
from prestodb import constants, exceptions
from prestodb.transaction import NO_TRANSACTION


__all__ = ["PrestoQuery", "PrestoRequest"]


logger = logging.getLogger(__name__)


MAX_ATTEMPTS = constants.DEFAULT_MAX_ATTEMPTS
SOCKS_PROXY = os.environ.get("SOCKS_PROXY")
if SOCKS_PROXY:
    PROXIES = {"http": "socks5://" + SOCKS_PROXY, "https": "socks5://" + SOCKS_PROXY}
else:
    PROXIES = None


class ClientSession(object):
    def __init__(
        self,
        user,
        catalog = None,
        schema = None,
        source = None,
        properties=None,
        headers=None,
        transaction_id=None,
    ):
        self.catalog = catalog
        self.schema = schema
        self.source = source
        self.user = user
        if properties is None:
            properties = {}
        self._properties = properties
        self._headers = headers or {}
        self.transaction_id = transaction_id

    def __repr__(self):
        return f"ClientSession({self.catalog}, {self.schema}, {self.source}, {self.user}, {self._properties}, {self._headers}, {self.transaction_id})"

    @property
    def properties(self):
        return self._properties

    @property
    def headers(self):
        return self._headers


def get_header_values(headers, header):
    return [val.strip() for val in headers[header].split(",")]


def get_session_property_values(headers, header):
    kvs = get_header_values(headers, header)
    return [
        (k.strip(), parse.unquote(v.strip()))
        for k, v in (kv.split("=", 1) for kv in kvs)
    ]


class PrestoStatus(object):
    def __init__(self, id, stats, warnings, info_uri, next_uri, rows, columns=None):
        self.id = id
        self.stats = stats
        self.warnings = warnings
        self.info_uri = info_uri
        self.next_uri = next_uri
        self.rows = rows
        self.columns = columns

    def __repr__(self):
        return (
            "PrestoStatus("
            "id={}, stats={{...}}, warnings={}, info_uri={}, next_uri={}, rows=<count={}>"
            ")".format(
                self.id,
                len(self.warnings),
                self.info_uri,
                self.next_uri,
                len(self.rows),
            )
        )


class PrestoRequest(object):
    """
    Manage the HTTP requests of a Presto query.

    :param host: name of the coordinator
    :param port: TCP port to connect to the coordinator
    :param user: associated with the query. It is useful for access control
                 and query scheduling.
    :param source: associated with the query. It is useful for access
                   control and query scheduling.
    :param catalog: to query. The *catalog* is associated with a Presto
                    connector. This variable sets the default catalog used
                    by SQL statements. For example, if *catalog* is set
                    to ``some_catalog``, the SQL statement
                    ``SELECT * FROM some_schema.some_table`` will actually
                    query the table
                    ``some_catalog.some_schema.some_table``.
    :param schema: to query. The *schema* is a logical abstraction to group
                   table. This variable sets the default schema used by
                   SQL statements. For eample, if *schema* is set to
                   ``some_schema``, the SQL statement
                   ``SELECT * FROM some_table`` will actually query the
                   table ``some_catalog.some_schema.some_table``.
    :param session_properties: set specific Presto behavior for the current
                               session. Please refer to the output of
                               ``SHOW SESSION`` to check the available
                               properties.
    :param http_headers: HTTP headers to post/get in the HTTP requests
    :param http_scheme: "http" or "https"
    :param auth: class that manages user authentication. ``None`` means no
                 authentication.
    :max_attempts: maximum number of attempts when sending HTTP requests. An
                   attempt is an HTTP request. 5 attempts means 4 retries.
    :request_timeout: How long (in seconds) to wait for the server to send
                      data before giving up, as a float or a
                      ``(connect timeout, read timeout)`` tuple.

    The client initiates a query by sending an HTTP POST to the
    coordinator. It then gets a response back from the coordinator with:
    - An URI to query to get the status for the query and the remaining
      data
    - An URI to get more information about the execution of the query
    - Statistics about the current query execution

    Please refer to :class:`PrestoStatus` to access the status returned by
    :meth:`PrestoRequest.process`.

    When the client makes an HTTP request, it may encounter the following
    errors:
    - Connection or read timeout:
      - There is a network partition and TCP segments are
        either dropped or delayed.
      - The coordinator stalled because of an OS level stall (page allocation
        stall, long time to page in pages, etc...), a JVM stall (full GC), or
        an application level stall (thread starving, lock contention)
    - Connection refused: Configuration or runtime issue on the coordinator
    - Connection closed:

    As most of these errors are transient, the question the caller should set
    retries with respect to when they want to notify the application that uses
    the client.
    """

    http = requests

    HTTP_EXCEPTIONS = (
        http.ConnectionError,  # type: ignore
        http.Timeout,  # type: ignore
    )

    def __init__(
        self,
        host,  # type: Text
        port,  # type: int
        client_session,  # type: ClientSession
        http_session=None,  # type: Any
        http_headers=None,  # type: Optional[Dict[Text, Text]]
        transaction_id=NO_TRANSACTION,  # type: Optional[Text]
        http_scheme=constants.HTTP,  # type: Text
        auth=constants.DEFAULT_AUTH,  # type: Optional[Any]
        redirect_handler=prestodb.redirect.GatewayRedirectHandler(),
        max_attempts=MAX_ATTEMPTS,  # type: int
        request_timeout=constants.DEFAULT_REQUEST_TIMEOUT,  # type: Union[float, Tuple[float, float]]
        handle_retry=exceptions.RetryWithExponentialBackoff(),
        service_account_file=None,
    ):
        # type: (...) -> None
        self._client_session = client_session
        self._host = host
        self._port = port
        self._next_uri = None  # type: Optional[Text]

        if http_session is not None:
            self._http_session = http_session
        else:
            # mypy cannot follow module import
            self._http_session = self.http.Session()  # type: ignore

        self.credentials = None
        self.auth_req = None
        if service_account_file is not None:
            import google.auth.transport.requests
            from google.oauth2 import service_account

            self.auth_req = google.auth.transport.requests.Request()
            self.credentials = service_account.Credentials.from_service_account_file(
                service_account_file, scopes=[constants.GCS_READ_ONLY]
            )
            self._http_session.headers.update(self.get_oauth_token())

        self.prepared_statements = []
        self._http_session.headers.update(self.http_headers)
        self._exceptions = self.HTTP_EXCEPTIONS
        self._auth = auth
        if self._auth:
            if http_scheme == constants.HTTP:
                raise ValueError("cannot use authentication with HTTP")
            self._auth.set_http_session(self._http_session)
            self._exceptions += self._auth.get_exceptions()

        self._redirect_handler = redirect_handler
        self._request_timeout = request_timeout
        self._handle_retry = handle_retry
        self.max_attempts = max_attempts
        self._http_scheme = http_scheme

    @property
    def transaction_id(self):
        return self._client_session.transaction_id

    @transaction_id.setter
    def transaction_id(self, value):
        self._client_session.transaction_id = value

    @property
    def http_headers(self):
        # type: () -> Dict[Text, Text]
        headers = {}

        headers[constants.HEADER_CATALOG] = self._client_session.catalog
        headers[constants.HEADER_SCHEMA] = self._client_session.schema
        headers[constants.HEADER_SOURCE] = self._client_session.source
        headers[constants.HEADER_USER] = self._client_session.user
        if len(self.prepared_statements) > 0:
            headers[constants.HEADER_PREPARED_STATEMENT] = ",".join(self.prepared_statements)

        headers[constants.HEADER_SESSION] = ",".join(
            # ``name`` must not contain ``=``
            "{}={}".format(name, parse.quote(str(value)))
            for name, value in self._client_session.properties.items()
        )

        # merge custom http headers
        for key in self._client_session.headers:
            if key in headers.keys():
                raise ValueError("cannot override reserved HTTP header {}".format(key))
        headers.update(self._client_session.headers)

        transaction_id = self._client_session.transaction_id
        headers[constants.HEADER_TRANSACTION] = transaction_id

        return headers

    @property
    def max_attempts(self):
        # type: () -> int
        return self._max_attempts

    @max_attempts.setter
    def max_attempts(self, value):
        # type: (int) -> None
        self._max_attempts = value
        if value == 1:  # No retry
            self._get = self._http_session.get
            self._post = self._http_session.post
            self._delete = self._http_session.delete
            return

        with_retry = exceptions.retry_with(
            self._handle_retry,
            exceptions=self._exceptions,
            conditions=(
                # need retry when there is no exception but the status code is 503
                lambda response: getattr(response, "status_code", None)
                == 503,
            ),
            max_attempts=self._max_attempts,
        )
        self._get = with_retry(self._http_session.get)
        self._post = with_retry(self._http_session.post)
        self._delete = with_retry(self._http_session.delete)

    def get_url(self, path):
        # type: (Text) -> Text
        return "{protocol}://{host}:{port}{path}".format(
            protocol=self._http_scheme, host=self._host, port=self._port, path=path
        )

    @property
    def statement_url(self):
        # type: () -> Text
        return self.get_url(constants.URL_STATEMENT_PATH)

    @property
    def next_uri(self):
        # type: () -> Text
        return self._next_uri

    def post(self, sql):
        data = sql.encode("utf-8")
        http_headers = self.http_headers

        http_response = self._post(
            self.statement_url,
            data=data,
            headers=http_headers,
            timeout=self._request_timeout,
            allow_redirects=self._redirect_handler is None,
            proxies=PROXIES,
        )
        if self._redirect_handler is not None:
            while http_response is not None and http_response.is_redirect:
                location = http_response.headers["Location"]
                url = self._redirect_handler.handle(location)
                logger.info(
                    "redirect {} from {} to {}".format(
                        http_response.status_code, location, url
                    )
                )
                http_response = self._post(
                    url,
                    data=data,
                    headers=http_headers,
                    timeout=self._request_timeout,
                    allow_redirects=False,
                    proxies=PROXIES,
                )
        return http_response

    def get(self, url):
        return self._get(
            url,
            headers=self.http_headers,
            timeout=self._request_timeout,
            proxies=PROXIES,
        )

    def delete(self, url):
        return self._delete(url, timeout=self._request_timeout, proxies=PROXIES)

    def _process_error(self, error, query_id):
        error_type = error["errorType"]
        if error_type == "EXTERNAL":
            raise exceptions.PrestoExternalError(error, query_id)
        elif error_type == "USER_ERROR":
            return exceptions.PrestoUserError(error, query_id)

        return exceptions.PrestoQueryError(error, query_id)

    def raise_response_error(self, http_response):
        if http_response.status_code == 503:
            raise exceptions.Http503Error("error 503: service unavailable")

        raise exceptions.HttpError(
            "error {}{}".format(
                http_response.status_code,
                ": {}".format(http_response.content) if http_response.content else "",
            )
        )

    def process(self, http_response):
        # type: (requests.Response) -> PrestoStatus
        if not http_response.ok:
            self.raise_response_error(http_response)

        http_response.encoding = "utf-8"
        response = http_response.json()
        if "error" in response:
            raise self._process_error(response["error"], response.get("id"))

        if constants.HEADER_CLEAR_SESSION in http_response.headers:
            for prop in get_header_values(
                http_response.headers, constants.HEADER_CLEAR_SESSION
            ):
                self._client_session.properties.pop(prop, None)

        if constants.HEADER_SET_SESSION in http_response.headers:
            for key, value in get_session_property_values(
                http_response.headers, constants.HEADER_SET_SESSION
            ):
                self._client_session.properties[key] = value

        if constants.HEADER_ADDED_PREPARE in http_response.headers:
            self._http_session.headers[
                constants.HEADER_PREPARED_STATEMENT
            ] = http_response.headers[constants.HEADER_ADDED_PREPARE]

        self._next_uri = response.get("nextUri")

        return PrestoStatus(
            id=response["id"],
            stats=response["stats"],
            warnings=response.get("warnings", []),
            info_uri=response["infoUri"],
            next_uri=self._next_uri,
            rows=response.get("data", []),
            columns=response.get("columns"),
        )

    @property
    def http_session(self):
        return self._http_session

    def get_oauth_token(self):
        self.credentials.refresh(self.auth_req)
        return {
            constants.PRESTO_EXTRA_CREDENTIAL: "%s = %s"
            % (constants.GCS_CREDENTIALS_OAUTH_TOKEN_KEY, self.credentials.token)
        }


class PrestoResult(object):
    """
    Represent the result of a Presto query as an iterator on rows.

    This class implements the iterator protocol as a generator type
    https://docs.python.org/3/library/stdtypes.html#generator-types
    """

    def __init__(self, query, rows=None, experimental_python_types = False):
        self._query = query
        self._rows = rows or []
        self._rownumber = 0
        self._experimental_python_types = experimental_python_types

    @property
    def rownumber(self):
        # type: () -> int
        return self._rownumber

    def __iter__(self):
        # Initial fetch from the first POST request
        for row in self._rows:
            self._rownumber += 1
            if not self._experimental_python_types:
                yield row
            else:
                yield self._map_to_python_types(row, self._query.columns)
        self._rows = None

        # Subsequent fetches from GET requests until next_uri is empty.
        while not self._query.is_finished():
            rows = self._query.fetch()
            for row in rows:
                self._rownumber += 1
                if not self._experimental_python_types:
                    yield row
                else:
                    yield self._map_to_python_types(row, self._query.columns)

    @classmethod
    def _map_to_python_type(cls, item: Tuple[Any, Dict]) -> Any:
        (value, data_type) = item

        if value is None:
            return None

        raw_type = data_type["typeSignature"]["rawType"]

        try:
            if isinstance(value, list):
                raw_type = {
                    "typeSignature": data_type["typeSignature"]["arguments"][0]["value"]
                }
                return [cls._map_to_python_type((array_item, raw_type)) for array_item in value]
            elif "decimal" in raw_type:
                return Decimal(value)
            elif raw_type == "date":
                return datetime.strptime(value, "%Y-%m-%d").date()
            elif raw_type == "timestamp with time zone":
                dt, tz = value.rsplit(' ', 1)
                if tz.startswith('+') or tz.startswith('-'):
                    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f %z")
                return datetime.strptime(dt, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=pytz.timezone(tz))
            elif "timestamp" in raw_type:
                return datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f")
            elif "time with time zone" in raw_type:
                matches = re.match(r'^(.*)([\+\-])(\d{2}):(\d{2})$', value)
                assert matches is not None
                assert len(matches.groups()) == 4
                if matches.group(2) == '-':
                    tz = -timedelta(hours=int(matches.group(3)), minutes=int(matches.group(4)))
                else:
                    tz = timedelta(hours=int(matches.group(3)), minutes=int(matches.group(4)))
                return datetime.strptime(matches.group(1), "%H:%M:%S.%f").time().replace(tzinfo=timezone(tz))
            elif "time" in raw_type:
                return datetime.strptime(value, "%H:%M:%S.%f").time()
            else:
                return value
        except ValueError as e:
            error_str = f"Could not convert '{value}' into the associated python type for '{raw_type}'"
            raise prestodb/client.py (error_str) from e

    def _map_to_python_types(self, row: List[Any], columns: List[Dict[str, Any]]) -> List[Any]:
        return list(map(self._map_to_python_type, zip(row, columns)))


class PrestoQuery(object):
    """Represent the execution of a SQL statement by Presto."""

    def __init__(
        self,
        request,  # type: PrestoRequest
        sql,  # type: Text
        experimental_python_types = False,
    ):
        # type: (...) -> None
        self.auth_req = request.auth_req  # type: Optional[Request]
        self.credentials = request.credentials  # type: Optional[Credentials]
        self.query_id = None  # type: Optional[Text]

        self._stats = {}  # type: Dict[Any, Any]
        self._warnings = []  # type: List[Dict[Any, Any]]
        self._columns = None  # type: Optional[List[Text]]
        self._finished = False
        self._cancelled = False
        self._request = request
        self._sql = sql
        self._result = PrestoResult(self, experimental_python_types=experimental_python_types)
        self._experimental_python_types = experimental_python_types

    @property
    def columns(self):
        return self._columns

    @property
    def stats(self):
        return self._stats

    @property
    def warnings(self):
        return self._warnings

    @property
    def result(self):
        return self._result

    def execute(self):
        # type: () -> PrestoResult
        """Initiate a Presto query by sending the SQL statement

        This is the first HTTP request sent to the coordinator.
        It sets the query_id and returns a Result object used to
        track the rows returned by the query. To fetch all rows,
        call fetch() until is_finished is true.
        """
        if self._cancelled:
            raise exceptions.PrestoUserError("Query has been cancelled", self.query_id)

        if self.credentials is not None and not self.credentials.valid:
            self._request.http_session.headers.update(self._request.get_oauth_token())

        response = self._request.post(self._sql)
        status = self._request.process(response)
        self.query_id = status.id
        self._stats.update({"queryId": self.query_id})
        self._stats.update(status.stats)
        self._warnings = getattr(status, "warnings", [])
        if status.next_uri is None:
            self._finished = True
        self._result = PrestoResult(self, status.rows, self._experimental_python_types)
        while (
            not self._finished and not self._cancelled
        ):
            self._result._rows += self.fetch()
        return self._result

    def fetch(self):
        # type: () -> List[List[Any]]
        """Continue fetching data for the current query_id"""
        if self._request.next_uri is None:
            self._finished = True
            return []
        response = self._request.get(self._request.next_uri)
        status = self._request.process(response)
        if status.columns:
            self._columns = status.columns
        self._stats.update(status.stats)
        if status.next_uri is None:
            self._finished = True
        return status.rows

    def cancel(self):
        # type: () -> None
        """Cancel the current query"""
        if self.query_id is None or self.is_finished():
            return

        self._cancelled = True
        url = self._request.get_url("/v1/query/{}".format(self.query_id))
        logger.debug("cancelling query: %s", self.query_id)
        response = self._request.delete(url)
        logger.info(response)
        if response.status_code == requests.codes.no_content:
            logger.debug("query cancelled: %s", self.query_id)
            return
        self._request.raise_response_error(response)

    def is_finished(self):
        # type: () -> bool
        return self._finished
