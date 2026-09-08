"""
Hand-rolled MCP (Model Context Protocol) over plain JSON-RPC 2.0.

Why not the official python-sdk?  It is asyncio/starlette based and does not
mount into a gevent WSGI server without a lot of glue.  Streamable HTTP
without SSE is just 'POST json, get json back', which is roughly 150 lines --
so we do that and keep the dependency list untouched.

Stateless: no sessions, no SSE.  Implemented methods:
initialize, notifications/*, tools/list, tools/call, ping.
"""

import json

PROTOCOL_VERSION = '2025-06-18'

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ToolError(Exception):
    """Raised by a tool handler to signal a domain error to the model."""


def _error(request_id, code, message, data=None):
    err = {'code': code, 'message': message}
    if data is not None:
        err['data'] = data
    return {'jsonrpc': '2.0', 'id': request_id, 'error': err}


def _result(request_id, result):
    return {'jsonrpc': '2.0', 'id': request_id, 'result': result}


def _text_content(payload):
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return {'content': [{'type': 'text', 'text': text}]}


class MCPServer:
    """
    Registry of tools plus a JSON-RPC dispatcher.

    A tool handler receives the *arguments* dict and returns any
    JSON-serialisable value (serialised into a single text content block).
    Raise ToolError for expected failures -- those come back with
    isError set so the model can react instead of the request blowing up.
    """

    def __init__(self, server_name='sms', server_version='4.0.0', instructions=None):
        self.server_name = server_name
        self.server_version = server_version
        self.instructions = instructions
        self._tools = []
        self._handlers = {}

    def tool(self, name, description, input_schema, handler):
        self._tools.append({
            'name': name,
            'description': description,
            'inputSchema': input_schema,
        })
        self._handlers[name] = handler

    def tool_names(self):
        return [t['name'] for t in self._tools]

    # -- JSON-RPC ----------------------------------------------------------

    def handle_raw(self, body):
        """
        Take a raw request body (bytes/str) and return
        (response_or_None, http_status).

        None means 'notification only, nothing to send back' and the caller
        should answer 202 with an empty body.
        """
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            return _error(None, PARSE_ERROR, 'parse error'), 200

        if isinstance(payload, list):
            responses = []
            for item in payload:
                resp, _ = self._handle_single(item)
                if resp is not None:
                    responses.append(resp)
            return (responses or None), 200

        return self._handle_single(payload)

    def _handle_single(self, payload):
        if not isinstance(payload, dict) or payload.get('jsonrpc') != '2.0':
            return _error(None, INVALID_REQUEST, 'invalid request'), 200

        request_id = payload.get('id')
        method = payload.get('method')
        params = payload.get('params') or {}

        if not isinstance(method, str):
            return _error(request_id, INVALID_REQUEST, 'missing method'), 200
        if not isinstance(params, dict):
            return _error(request_id, INVALID_PARAMS, 'params must be an object'), 200

        # notifications carry no id and expect no response
        is_notification = 'id' not in payload

        if method == 'initialize':
            result = {
                'protocolVersion': PROTOCOL_VERSION,
                'capabilities': {'tools': {'listChanged': False}},
                'serverInfo': {'name': self.server_name, 'version': self.server_version},
            }
            if self.instructions:
                result['instructions'] = self.instructions
            return _result(request_id, result), 200

        if method.startswith('notifications/'):
            return None, 202

        if method == 'ping':
            return _result(request_id, {}), 200

        if method == 'tools/list':
            return _result(request_id, {'tools': self._tools}), 200

        if method == 'tools/call':
            name = params.get('name')
            arguments = params.get('arguments') or {}
            if not isinstance(name, str) or name not in self._handlers:
                return _error(request_id, INVALID_PARAMS, 'unknown tool: {}'.format(name)), 200
            if not isinstance(arguments, dict):
                return _error(request_id, INVALID_PARAMS, 'arguments must be an object'), 200
            try:
                value = self._handlers[name](arguments)
            except ToolError as exc:
                out = _text_content({'error': str(exc)})
                out['isError'] = True
                return _result(request_id, out), 200
            except Exception as exc:  # report, don't kill the request
                out = _text_content({'error': '{}: {}'.format(type(exc).__name__, exc)})
                out['isError'] = True
                return _result(request_id, out), 200
            return _result(request_id, _text_content(value)), 200

        if is_notification:
            return None, 202
        return _error(request_id, METHOD_NOT_FOUND, 'method not found: {}'.format(method)), 200
